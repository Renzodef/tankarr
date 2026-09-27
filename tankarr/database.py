from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import local
from time import monotonic, time_ns
from typing import Any

from tankarr import source_health
from tankarr.catalogue import NO_REMOTE_PROVIDERS
from tankarr.chapter_map import MapEntry, coverage_for_chapter
from tankarr.chapter_mapping import (
    canonical_number,
    counted_chapter_total,
    logical_release_key,
    volume_scoped_numbering,
)
from tankarr.download_sources import DOWNLOAD_PROVIDER_PRIORITY_DEFAULT
from tankarr.library_snapshot import (
    CALENDAR_REVISION_TABLES,
    CHAPTER_SUMMARY_COLUMNS,
    LIBRARY_REVISION_TABLES,
    WANTED_REVISION_TABLES,
    decode_chapter_summary,
    install_revision_triggers,
)
from tankarr.numbering_reconciliation import reconcile_numbering
from tankarr.series_summary import publication_summary
from tankarr.series_unit import is_volume_release, select_releases
from tankarr.source_numbering import numbered_prologue
from tankarr.source_ranking import (
    SourceRanking,
    beyond_frontier,
    official_frontier,
    official_hosts,
    official_source_roles,
    release_host,
    release_source_keys,
)
from tankarr.torrent_sources import EXTERNAL_IMPORT_PROVIDERS, TORRENT_IMPORT_PROVIDERS

SCHEMA = """
CREATE TABLE IF NOT EXISTS manga (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    metadata_title TEXT,
    title_override TEXT,
    authors_override_json TEXT,
    description TEXT NOT NULL DEFAULT '',
    cover_url TEXT,
    authors_json TEXT NOT NULL DEFAULT '[]',
    creator_links_json TEXT NOT NULL DEFAULT '[]',
    original_language TEXT,
    status TEXT,
    status_override TEXT,
    library_status_override TEXT,
    verified_chapter_count INTEGER,
    verified_chapter_source TEXT,
    verified_chapter_checked_at TEXT,
    expected_count_override INTEGER,
    expected_count_unit_override TEXT,
    year INTEGER,
    last_volume TEXT,
    last_chapter TEXT,
    available_languages_json TEXT NOT NULL DEFAULT '[]',
    external_correlations_json TEXT NOT NULL DEFAULT '[]',
    source_url TEXT,
    source_name TEXT,
    source_id TEXT,
    reader_mode_override TEXT,
    reader_direction_override TEXT,
    preferred_language TEXT NOT NULL DEFAULT 'en',
    monitor_mode TEXT NOT NULL DEFAULT 'none',
    future_monitoring_allowed INTEGER NOT NULL DEFAULT 1,
    future_monitoring_reason TEXT NOT NULL DEFAULT '',
    monitored INTEGER NOT NULL DEFAULT 1,
    auto_download INTEGER NOT NULL DEFAULT 0,
    assemble_books_automatically INTEGER NOT NULL DEFAULT 0,
    translation_enabled INTEGER NOT NULL DEFAULT 0,
    translation_source_languages TEXT NOT NULL DEFAULT '',
    monitor_initialized INTEGER NOT NULL DEFAULT 0,
    last_checked_at TEXT,
    last_check_error TEXT,
    primary_source_error TEXT,
    primary_source_error_since TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapter_release (
    id TEXT PRIMARY KEY,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    volume TEXT,
    chapter TEXT,
    source_chapter TEXT,
    edition_chapter TEXT,
    canonical_chapter TEXT,
    primary_chapter TEXT,
    numbering_status TEXT NOT NULL DEFAULT 'mapped',
    numbering_method TEXT NOT NULL DEFAULT 'legacy',
    numbering_confidence REAL NOT NULL DEFAULT 0.5,
    numbering_evidence_json TEXT NOT NULL DEFAULT '{}',
    numbering_version INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL,
    provider TEXT NOT NULL,
    groups_json TEXT NOT NULL DEFAULT '[]',
    publish_at TEXT,
    source_url TEXT NOT NULL,
    pages INTEGER,
    version INTEGER,
    downloaded INTEGER NOT NULL DEFAULT 0,
    library_path TEXT,
    library_sha256 TEXT,
    local_import_sha256 TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chapter_manga_language
    ON chapter_release(manga_id, language, publish_at);

CREATE TABLE IF NOT EXISTS translation_job (
    id TEXT PRIMARY KEY,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    slot_key TEXT NOT NULL,
    source_json TEXT NOT NULL,
    target_language TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    message TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    result_chapter_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(manga_id, slot_key, target_language)
);

CREATE TABLE IF NOT EXISTS manga_release_source (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_manga_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_url TEXT,
    source_name TEXT,
    language TEXT NOT NULL,
    match_confidence REAL NOT NULL,
    match_reason TEXT NOT NULL,
    verified_by TEXT NOT NULL,
    source_role TEXT NOT NULL DEFAULT 'alternative',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_checked_at TEXT,
    last_error TEXT,
    error_since TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, provider, provider_manga_id)
);

CREATE INDEX IF NOT EXISTS idx_manga_release_source_manga
    ON manga_release_source(manga_id, enabled, provider);

CREATE TABLE IF NOT EXISTS manga_official_source (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    host TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL,
    source_role TEXT NOT NULL CHECK(
        source_role IN ('primary_official', 'secondary_official', 'alternative')
    ),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, source_url)
);

CREATE INDEX IF NOT EXISTS idx_manga_official_source_manga
    ON manga_official_source(manga_id, source_role, host);

CREATE TABLE IF NOT EXISTS official_edition_evidence (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    host TEXT NOT NULL,
    edition_language TEXT NOT NULL,
    provider_index TEXT NOT NULL,
    edition_chapter TEXT,
    title TEXT NOT NULL DEFAULT '',
    publish_at TEXT,
    source_url TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    checked_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, host, provider_index)
);

CREATE INDEX IF NOT EXISTS idx_official_edition_evidence_manga
    ON official_edition_evidence(manga_id, host, edition_language);

CREATE TABLE IF NOT EXISTS series_chapter_map (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    volumes TEXT NOT NULL,
    chapters TEXT NOT NULL,
    exact INTEGER NOT NULL DEFAULT 1,
    release_date TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, source, volumes, chapters)
);

CREATE TABLE IF NOT EXISTS release_numbering_override (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    release_id TEXT PRIMARY KEY REFERENCES chapter_release(id) ON DELETE CASCADE,
    source_chapter TEXT NOT NULL,
    canonical_chapter TEXT NOT NULL,
    evidence TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_release_history (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    chapter TEXT NOT NULL,
    volume TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, source, chapter, volume, release_date)
);

CREATE TABLE IF NOT EXISTS release_source_rejection (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_manga_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, provider, provider_manga_id)
);

CREATE TABLE IF NOT EXISTS publication_pause (
    manga_id TEXT PRIMARY KEY REFERENCES manga(id) ON DELETE CASCADE,
    paused INTEGER NOT NULL DEFAULT 0,
    sources_json TEXT NOT NULL DEFAULT '[]',
    changed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS release_block (
    chapter_id TEXT PRIMARY KEY REFERENCES chapter_release(id) ON DELETE CASCADE,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dismissed_alert (
    key TEXT PRIMARY KEY,
    signature TEXT NOT NULL,
    dismissed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_failure (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    transient_failures INTEGER NOT NULL DEFAULT 0,
    last_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, source_key)
);

CREATE TABLE IF NOT EXISTS wanted_attempt (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    slot_key TEXT NOT NULL,
    channel TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    first_attempted_at TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, slot_key, channel)
);

CREATE INDEX IF NOT EXISTS idx_wanted_attempt_manga
    ON wanted_attempt(manga_id, slot_key);

CREATE TABLE IF NOT EXISTS page_quality (
    chapter_id TEXT PRIMARY KEY REFERENCES chapter_release(id) ON DELETE CASCADE,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    verdict TEXT NOT NULL,
    normalized_height INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    measured_pages INTEGER NOT NULL DEFAULT 0,
    median_aspect REAL NOT NULL DEFAULT 0,
    max_width INTEGER NOT NULL DEFAULT 0,
    median_width INTEGER NOT NULL DEFAULT 0,
    library_sha256 TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    measured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_page_signatures (
    library_sha256 TEXT PRIMARY KEY,
    pages INTEGER NOT NULL DEFAULT 0,
    signatures TEXT NOT NULL DEFAULT '',
    computed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapter_content_alignment (
    chapter_id TEXT PRIMARY KEY REFERENCES chapter_release(id) ON DELETE CASCADE,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    volume TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL,
    matched_pages INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    min_distance INTEGER,
    chapter_sha256 TEXT NOT NULL DEFAULT '',
    book_sha256 TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chapter_content_alignment_manga
    ON chapter_content_alignment(manga_id);

CREATE INDEX IF NOT EXISTS idx_page_quality_manga
    ON page_quality(manga_id, verdict);

CREATE TABLE IF NOT EXISTS source_health (
    source_key TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    ema_success REAL NOT NULL DEFAULT 0.5,
    ema_speed_bps REAL NOT NULL DEFAULT 0,
    last_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_circuit (
    source_key TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL DEFAULT 0,
    failure_code TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS download_job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manga_id TEXT NOT NULL,
    chapter_id TEXT NOT NULL,
    requested_language TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'automatic',
    progress REAL NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    result_path TEXT,
    planned_path TEXT,
    language_evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS torrent_download (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    info_hash TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    language TEXT NOT NULL,
    category TEXT NOT NULL,
    indexer TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    seeders INTEGER NOT NULL DEFAULT 0,
    leechers INTEGER NOT NULL DEFAULT 0,
    trusted INTEGER NOT NULL DEFAULT 0,
    remake INTEGER NOT NULL DEFAULT 0,
    volume_hint TEXT,
    chapter_hint TEXT,
    publish_at TEXT,
    source_url TEXT NOT NULL,
    torrent_url TEXT NOT NULL,
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0,
    qbit_state TEXT,
    content_path TEXT,
    imported_paths_json TEXT NOT NULL DEFAULT '[]',
    language_evidence_json TEXT NOT NULL DEFAULT '{}',
    message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(manga_id, source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_download_job_status_manga
    ON download_job(status, manga_id);

CREATE INDEX IF NOT EXISTS idx_torrent_download_manga
    ON torrent_download(manga_id, updated_at);

CREATE INDEX IF NOT EXISTS idx_torrent_download_status
    ON torrent_download(status, updated_at);

CREATE TABLE IF NOT EXISTS match_review (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    provider TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    title TEXT NOT NULL,
    source_name TEXT,
    source_url TEXT,
    confidence REAL NOT NULL DEFAULT 0,
    reason TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT,
    UNIQUE(manga_id, kind, candidate_id)
);

CREATE TABLE IF NOT EXISTS indexer_offer (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    volume TEXT NOT NULL,
    protocol TEXT NOT NULL,
    title TEXT NOT NULL,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    seen_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, volume, protocol, title)
);

CREATE TABLE IF NOT EXISTS deletion_operation (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'delete',
    replacement_chapter_id TEXT,
    replacement_sha256 TEXT,
    manifest_path TEXT NOT NULL,
    library_paths_json TEXT NOT NULL DEFAULT '[]',
    komga_attempts INTEGER NOT NULL DEFAULT 0,
    komga_last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series_deletion_request (
    id TEXT PRIMARY KEY,
    manga_id TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after REAL NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS setting (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reader_bookmark (
    manga_id TEXT PRIMARY KEY REFERENCES manga(id) ON DELETE CASCADE,
    chapter_id TEXT NOT NULL REFERENCES chapter_release(id) ON DELETE CASCADE,
    page_index INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS metadata_source_record (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_key TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    match_confidence REAL NOT NULL,
    match_reason TEXT NOT NULL DEFAULT '',
    data_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, entity_type, entity_key, source)
);

CREATE TABLE IF NOT EXISTS metadata_manual_correlation (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, source)
);

CREATE TABLE IF NOT EXISTS metadata_provider_correlation (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    label TEXT NOT NULL,
    origin_provider TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, source)
);

CREATE TABLE IF NOT EXISTS series_metadata (
    manga_id TEXT PRIMARY KEY REFERENCES manga(id) ON DELETE CASCADE,
    data_json TEXT NOT NULL,
    artwork_path TEXT,
    artwork_sha256 TEXT,
    artwork_media_type TEXT,
    source_status_json TEXT NOT NULL DEFAULT '[]',
    last_enriched_at TEXT NOT NULL,
    last_synced_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS series_artwork_candidate (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_url TEXT NOT NULL,
    artwork_path TEXT NOT NULL,
    artwork_sha256 TEXT NOT NULL,
    artwork_media_type TEXT NOT NULL,
    source_width INTEGER NOT NULL,
    source_height INTEGER NOT NULL,
    score REAL NOT NULL,
    source_priority INTEGER NOT NULL,
    is_automatic INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS series_artwork_preference (
    manga_id TEXT PRIMARY KEY REFERENCES manga(id) ON DELETE CASCADE,
    candidate_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volume_metadata (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    volume_key TEXT NOT NULL,
    data_json TEXT NOT NULL,
    artwork_path TEXT,
    artwork_sha256 TEXT,
    artwork_media_type TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, volume_key)
);

CREATE TABLE IF NOT EXISTS volume_monitor_override (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    volume_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('monitored', 'ignored')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, volume_key)
);

CREATE TABLE IF NOT EXISTS wanted_search_state (
    manga_id TEXT PRIMARY KEY REFERENCES manga(id) ON DELETE CASCADE,
    last_search_at TEXT,
    next_search_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_found_at TEXT
);

CREATE TABLE IF NOT EXISTS metadata_sync_state (
    target_kind TEXT NOT NULL,
    target_id TEXT NOT NULL,
    payload_sha256 TEXT,
    artwork_sha256 TEXT,
    synced_at TEXT NOT NULL,
    last_error TEXT,
    PRIMARY KEY (target_kind, target_id)
);

CREATE TABLE IF NOT EXISTS author (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    merged_into TEXT REFERENCES author(id),
    last_refreshed_at TEXT,
    next_refresh_at TEXT,
    refresh_failures INTEGER NOT NULL DEFAULT 0,
    last_refresh_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS author_alias (
    author_id TEXT NOT NULL REFERENCES author(id) ON DELETE CASCADE,
    normalized_name TEXT NOT NULL,
    name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (author_id, normalized_name)
);

CREATE TABLE IF NOT EXISTS mangabaka_work (
    catalogue_id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS author_work (
    author_id TEXT NOT NULL REFERENCES author(id) ON DELETE CASCADE,
    catalogue_id TEXT NOT NULL REFERENCES mangabaka_work(catalogue_id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (author_id, catalogue_id)
);

CREATE TABLE IF NOT EXISTS manga_author (
    manga_id TEXT NOT NULL REFERENCES manga(id) ON DELETE CASCADE,
    author_id TEXT NOT NULL REFERENCES author(id),
    credited_names_json TEXT NOT NULL DEFAULT '[]',
    roles_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (manga_id, author_id)
);

CREATE TABLE IF NOT EXISTS author_merge (
    source_author_id TEXT PRIMARY KEY REFERENCES author(id),
    target_author_id TEXT NOT NULL REFERENCES author(id),
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    merged_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_metadata_source_manga
    ON metadata_source_record(manga_id, entity_type, entity_key);

CREATE INDEX IF NOT EXISTS idx_series_metadata_enriched
    ON series_metadata(last_enriched_at);

CREATE INDEX IF NOT EXISTS idx_series_artwork_candidate_manga
    ON series_artwork_candidate(manga_id, is_automatic, source_priority, score);

CREATE INDEX IF NOT EXISTS idx_author_due
    ON author(merged_into, next_refresh_at);

CREATE INDEX IF NOT EXISTS idx_author_alias_name
    ON author_alias(normalized_name);

CREATE INDEX IF NOT EXISTS idx_author_work_catalogue
    ON author_work(catalogue_id, author_id);

CREATE INDEX IF NOT EXISTS idx_manga_author_author
    ON manga_author(author_id, manga_id);
"""

# Download order shared by the worker and the Activity page: active jobs
# first, then the series with the fewest chapters still queued (small works
# finish before a 900-chapter backlog starts), ties by earliest queued job,
# then chapter number inside the series.
# Per-series aggregates are computed once (CTE) instead of per row: with
# thousands of queued jobs the correlated form starved the event loop.
QUEUE_CTE = """
WITH queue_series AS (
    SELECT manga_id, COUNT(*) AS queued_n, MIN(id) AS first_queued_id
    FROM download_job WHERE status = 'queued' GROUP BY manga_id
)
"""
QUEUE_ORDER = """
    CASE job.status WHEN 'queued' THEN 1 ELSE 0 END,
    COALESCE(queue_series.queued_n, 0),
    COALESCE(queue_series.first_queued_id, 0),
    CAST(chapter.chapter AS REAL),
    job.id
"""

JOB_SELECT = """
SELECT
    job.*,
    COALESCE(
        NULLIF(trim(manga.title_override), ''),
        NULLIF(trim(manga.metadata_title), ''),
        manga.title
    ) AS manga_title,
    COALESCE(
        NULLIF(trim(manga.cover_url), ''),
        CASE
            WHEN NULLIF(trim(series_artwork.artwork_sha256), '') IS NOT NULL
            THEN '/api/metadata/artwork/' || manga.id || '/series?v='
                 || series_artwork.artwork_sha256
        END
    ) AS manga_cover_url,
    manga.source_name AS manga_source_name,
    manga.source_id AS manga_source_id,
    chapter.volume AS chapter_volume,
    chapter.chapter AS chapter_number,
    chapter.title AS chapter_title,
    chapter.groups_json AS chapter_groups_json,
    chapter.provider AS chapter_provider,
    chapter.source_name AS chapter_source_name,
    chapter.source_url AS chapter_source_url,
    source_gate.next_retry_at AS source_retry_at,
    source_gate.failure_code AS source_failure_code
FROM download_job AS job
LEFT JOIN manga ON manga.id = job.manga_id
LEFT JOIN series_metadata AS series_artwork ON series_artwork.manga_id = manga.id
LEFT JOIN chapter_release AS chapter ON chapter.id = job.chapter_id
LEFT JOIN source_circuit AS source_gate ON source_gate.source_key =
    source_gate_key(chapter.provider, chapter.source_url, chapter.source_name)
"""

TORRENT_SELECT = """
SELECT
    torrent.*,
    COALESCE(
        NULLIF(trim(manga.title_override), ''),
        NULLIF(trim(manga.metadata_title), ''),
        manga.title
    ) AS manga_title,
    COALESCE(
        NULLIF(trim(manga.cover_url), ''),
        CASE
            WHEN NULLIF(trim(series_artwork.artwork_sha256), '') IS NOT NULL
            THEN '/api/metadata/artwork/' || manga.id || '/series?v='
                 || series_artwork.artwork_sha256
        END
    ) AS manga_cover_url,
    manga.source_name AS manga_source_name
FROM torrent_download AS torrent
JOIN manga ON manga.id = torrent.manga_id
LEFT JOIN series_metadata AS series_artwork ON series_artwork.manga_id = manga.id
"""

ACTIVE_DOWNLOAD_STATUSES = ("running", "downloading", "packaging", "importing")
CANCELLABLE_DOWNLOAD_STATUSES = ("queued", "running", "downloading", "packaging")
ACTIVE_TORRENT_STATUSES = (
    "adding",
    "queued",
    "downloading",
    "checking",
    "completed",
    "importing",
    "review",
)
TORRENT_STATUSES = frozenset((*ACTIVE_TORRENT_STATUSES, "review", "imported", "failed"))


class ActiveDownloadJobsError(RuntimeError):
    def __init__(self, jobs: list[dict[str, Any]]):
        self.jobs = jobs
        summary = ", ".join(f"{job['id']} ({job['status']})" for job in jobs)
        super().__init__(
            f"Cannot delete while download jobs are active: {summary}. "
            "Wait for them to finish and try again."
        )


class TorrentDownloadsExistError(ActiveDownloadJobsError):
    def __init__(self, jobs: list[dict[str, Any]]):
        self.jobs = jobs
        summary = ", ".join(f"{job['id']} ({job['status']})" for job in jobs)
        RuntimeError.__init__(
            self,
            "Cannot delete this series while torrent/qBittorrent records exist: "
            f"{summary}. Remove them from Interactive search first; imported Comics "
            "files are kept unless you delete the series afterwards.",
        )


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_local_text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(normalized.split())


def _canonical_local_number(value: object) -> str | None:
    raw = _canonical_local_text(value)
    if not raw:
        return None
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError):
        return raw
    if not number.is_finite():
        return raw
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in {"-0", ""}:
        return "0"
    return normalized


# How a download refused by the page-quality gate is recorded (service.py).
QUALITY_REFUSAL_PREFIX = "DegradedPagesError"


def local_series_identity(title: object, authors: Iterable[object]) -> str:
    """Stable identity for local imports, independent of case and author order."""

    canonical_title = _canonical_local_text(title)
    canonical_authors = sorted(
        {
            normalized
            for author in authors
            if (normalized := _canonical_local_text(author))
        }
    )
    if not canonical_title:
        raise ValueError("A local series title is required")
    payload = json.dumps(
        [canonical_title, canonical_authors],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def local_release_identity(language: object, volume: object, chapter: object) -> str:
    """Stable logical key for one local volume/chapter in one language."""

    canonical_language = _canonical_local_text(language)
    canonical_volume = _canonical_local_number(volume)
    canonical_chapter = _canonical_local_number(chapter)
    if not canonical_language:
        raise ValueError("A local import language is required")
    if canonical_volume is None and canonical_chapter is None:
        raise ValueError("A local import requires a volume or chapter number")
    payload = json.dumps(
        [canonical_language, canonical_volume, canonical_chapter],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def logical_chapter_coverage(releases: Iterable[dict[str, Any]]) -> dict[str, int]:
    """Collapse provider parts into canonical chapter coverage counts.

    A work can expose chapter 28 as releases 28.1 and 28.2. Those are two
    physical books but one canonical chapter, while a lone 107.5 remains a
    special. Download coverage for a split chapter is complete only after all
    known parts are present.
    """

    numbered: dict[Decimal, bool] = {}
    unnumbered: dict[str, bool] = {}
    for release in releases:
        raw = str(release.get("chapter") or "").strip()
        if not raw:
            continue
        downloaded = bool(release.get("downloaded"))
        try:
            number = Decimal(raw)
        except (InvalidOperation, ValueError):
            key = _canonical_local_text(raw)
            unnumbered[key] = bool(unnumbered.get(key) or downloaded)
            continue
        if not number.is_finite() or number < 0:
            key = _canonical_local_text(raw)
            unnumbered[key] = bool(unnumbered.get(key) or downloaded)
            continue
        numbered[number] = bool(numbered.get(number) or downloaded)

    integers = {
        int(number): downloaded
        for number, downloaded in numbered.items()
        if number == number.to_integral_value()
    }
    fractional: dict[int, dict[Decimal, bool]] = {}
    for number, downloaded in numbered.items():
        if number == number.to_integral_value():
            continue
        fractional.setdefault(int(number), {})[number] = downloaded

    canonical_available = len(integers)
    canonical_downloaded = sum(int(downloaded) for downloaded in integers.values())
    special_available = len(unnumbered)
    special_downloaded = sum(int(downloaded) for downloaded in unnumbered.values())
    for chapter_base, parts in fractional.items():
        if chapter_base not in integers and len(parts) >= 2:
            canonical_available += 1
            canonical_downloaded += int(all(parts.values()))
        else:
            special_available += len(parts)
            special_downloaded += sum(int(downloaded) for downloaded in parts.values())

    return {
        "logical_chapter_count": canonical_available,
        "logical_downloaded_count": canonical_downloaded,
        "special_chapter_count": special_available,
        "special_downloaded_count": special_downloaded,
    }


logger = logging.getLogger(__name__)

REVIEW_REFUSED_BY_RULES = "rejected: not verified by the automatic rules"


class ReleaseBlocked(RuntimeError):
    """A release that failed before cannot be queued automatically."""

    def __init__(self, chapter_id: str):
        super().__init__(f"Release {chapter_id} is blocked after a failed download")
        self.chapter_id = chapter_id


class ReleaseSourceOwned(ValueError):
    """That entry on the download source already serves another series."""

    def __init__(self, message: str, *, owner_id: str = "", owner_title: str = ""):
        super().__init__(message)
        self.owner_id = owner_id
        self.owner_title = owner_title


# Sources decorate titles with their own state: a lock for a paid or early
# episode on Tapas/Webtoons ("🔒 251. New Message"), fire and "new" badges.
# The lock is information (source_numbering.is_locked_title reads it), so
# titles are stored verbatim; this helper is for display only.
_RELEASE_TITLE_NOISE = re.compile(
    "[\U0001f510-\U0001f513\U0001f525\U0001f195\U0001f51f\u2b50\U0001f451"
    "\U0001f48e\U0001f4b0\U0001f4b2\ufe0f\u200d]"
)


def clean_release_title(value: object) -> str:
    text = _RELEASE_TITLE_NOISE.sub("", str(value or ""))
    return " ".join(text.split()).strip(" -–—·:")


# Increment whenever initialize adds or changes a schema migration. The marker is
# committed only after initialization succeeds; upgrades are backed up beforehand.
SCHEMA_VERSION = 7


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._read_snapshot = local()
        # Operator-ordered download source priority. App wiring keeps it in
        # sync with Settings so release selection prefers trusted providers.
        self.provider_priority: tuple[str, ...] = DOWNLOAD_PROVIDER_PRIORITY_DEFAULT
        self.source_ranking: SourceRanking = SourceRanking.from_settings(
            provider_priority=DOWNLOAD_PROVIDER_PRIORITY_DEFAULT
        )

    @contextmanager
    def connect(self):
        snapshot = getattr(self._read_snapshot, "connection", None)
        if snapshot is not None:
            yield snapshot
            return
        connection = sqlite3.connect(self.path, timeout=30)
        from tankarr.source_circuit import source_gate_key

        connection.create_function(
            "source_gate_key", 3, source_gate_key, deterministic=True
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def read_snapshot(self):
        """Keep nested read helpers on one transaction and one connection.

        SQLite's default deferred transactions do not begin for SELECTs. A
        connection alone therefore does not make a multi-query read coherent.
        The explicit BEGIN pins the inputs and their cache revisions together
        while WAL still lets the downloader commit independently.
        """

        if getattr(self._read_snapshot, "connection", None) is not None:
            yield
            return
        with self.connect() as connection:
            connection.execute("BEGIN")
            self._read_snapshot.connection = connection
            try:
                yield
            finally:
                del self._read_snapshot.connection

    @contextmanager
    def write_snapshot(self):
        """Revalidate and change one operator decision in a single transaction."""

        if getattr(self._read_snapshot, "connection", None) is not None:
            raise RuntimeError("A write snapshot cannot upgrade a read transaction")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._read_snapshot.connection = connection
            try:
                yield
            finally:
                del self._read_snapshot.connection

    def initialize(self, *, before_migration: Callable[[], Any] | None = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            previous_version = connection.execute("PRAGMA user_version").fetchone()[0]
            existing = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
            ).fetchone()
            if existing and previous_version < SCHEMA_VERSION:
                if before_migration is not None:
                    before_migration()
                else:
                    from tankarr.backups import DatabaseBackups

                    DatabaseBackups(
                        self.path, self.path.parent / "backups", retain=7
                    ).create()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            self._ensure_column(
                connection, "manga", "translation_enabled", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                connection,
                "manga",
                "translation_source_languages",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(connection, "manga", "reader_mode_override", "TEXT")
            self._ensure_column(
                connection, "manga", "reader_direction_override", "TEXT"
            )
            self._ensure_column(
                connection,
                "manga",
                "assemble_books_automatically",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "chapter_release", "assembled_from", "TEXT")
            self._ensure_column(
                connection,
                "manga",
                "monitor_initialized",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "manga", "last_checked_at", "TEXT")
            self._ensure_column(
                connection,
                "chapter_release",
                "release_unit",
                "TEXT NOT NULL DEFAULT 'chapter'",
            )
            self._ensure_column(
                connection, "manga", "monitor_specials", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(connection, "manga", "series_unit_override", "TEXT")
            # A part of a split chapter remembers the chapter it belongs to,
            # so the parts are replaced by the whole and retired together.
            self._ensure_column(
                connection, "page_quality", "whole_chapter", "TEXT NOT NULL DEFAULT ''"
            )
            # A replacement names the file it supersedes and deletes it only
            # once it has itself passed every gate (ONE PIECE 554, live: the
            # old file went first, the replacement was refused, the chapter
            # was gone). An operator may also accept a short chapter.
            self._ensure_column(
                connection, "download_job", "supersedes_chapter_id", "TEXT"
            )
            self._ensure_column(
                connection,
                "download_job",
                "quality_override",
                "INTEGER NOT NULL DEFAULT 0",
            )
            # page_quality gained a second axis after its table already
            # existed in the field: shape, alongside how much a chapter
            # carries. CREATE TABLE IF NOT EXISTS cannot add it.
            self._ensure_column(
                connection,
                "page_quality",
                "measured_pages",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection, "page_quality", "median_aspect", "REAL NOT NULL DEFAULT 0"
            )
            self._repair_year_fraction_labels(connection)
            self._remeasure_page_quality_if_needed(connection)
            self._ensure_column(
                connection,
                "torrent_download",
                "protocol",
                "TEXT NOT NULL DEFAULT 'torrent'",
            )
            self._ensure_column(connection, "torrent_download", "client_id", "TEXT")
            # The operator's pick of which books to take, so a resumed or
            # automatic import obeys it instead of taking the whole pack.
            self._ensure_column(
                connection, "torrent_download", "selected_paths_json", "TEXT"
            )
            self._ensure_column(connection, "chapter_release", "source_key", "TEXT")
            self._ensure_column(connection, "chapter_release", "source_name", "TEXT")
            self._ensure_column(connection, "chapter_release", "source_chapter", "TEXT")
            self._ensure_column(
                connection, "chapter_release", "edition_chapter", "TEXT"
            )
            self._ensure_column(
                connection, "chapter_release", "canonical_chapter", "TEXT"
            )
            self._ensure_column(
                connection, "chapter_release", "primary_chapter", "TEXT"
            )
            self._ensure_column(
                connection,
                "manga_release_source",
                "source_role",
                "TEXT NOT NULL DEFAULT 'alternative'",
            )
            # What the source itself says about the work's publication
            # ("On hiatus" on Webtoons while the catalogue still says ongoing).
            self._ensure_column(
                connection, "manga_release_source", "publication_status", "TEXT"
            )
            for row in connection.execute(
                "SELECT manga_id FROM series_metadata"
            ).fetchall():
                self._refresh_official_edition_sources_connection(
                    connection, str(row["manga_id"])
                )
            self._ensure_column(
                connection,
                "chapter_release",
                "numbering_status",
                "TEXT NOT NULL DEFAULT 'mapped'",
            )
            self._ensure_column(
                connection,
                "chapter_release",
                "numbering_method",
                "TEXT NOT NULL DEFAULT 'legacy'",
            )
            self._ensure_column(
                connection,
                "chapter_release",
                "numbering_confidence",
                "REAL NOT NULL DEFAULT 0.5",
            )
            self._ensure_column(
                connection,
                "chapter_release",
                "numbering_evidence_json",
                "TEXT NOT NULL DEFAULT '{}'",
            )
            self._ensure_column(
                connection,
                "chapter_release",
                "numbering_version",
                "INTEGER NOT NULL DEFAULT 0",
            )
            connection.execute(
                """
                UPDATE chapter_release
                SET release_unit='volume',
                    numbering_status='mapped',
                    numbering_method='volume_identity',
                    numbering_confidence=1.0
                WHERE release_unit='chapter'
                  AND (chapter IS NULL OR trim(chapter)='')
                  AND (source_chapter IS NULL OR trim(source_chapter)='')
                  AND volume IS NOT NULL AND trim(volume)!=''
                """
            )
            connection.execute(
                "UPDATE chapter_release SET source_chapter=chapter "
                "WHERE source_chapter IS NULL AND chapter IS NOT NULL"
            )
            connection.execute(
                "UPDATE chapter_release SET edition_chapter=source_chapter "
                "WHERE edition_chapter IS NULL AND source_chapter IS NOT NULL"
            )
            connection.execute(
                "UPDATE chapter_release SET canonical_chapter=chapter "
                "WHERE canonical_chapter IS NULL AND chapter IS NOT NULL "
                "AND numbering_status='mapped'"
            )
            connection.execute(
                "UPDATE chapter_release SET primary_chapter=canonical_chapter "
                "WHERE primary_chapter IS NULL AND canonical_chapter IS NOT NULL "
                "AND numbering_status='mapped'"
            )
            self._ensure_column(connection, "manga", "last_check_error", "TEXT")
            self._ensure_column(connection, "manga", "primary_source_error", "TEXT")
            self._ensure_column(
                connection, "manga", "primary_source_error_since", "TEXT"
            )
            self._ensure_column(
                connection, "manga_release_source", "error_since", "TEXT"
            )
            if previous_version < 3:
                connection.execute(
                    "UPDATE manga_release_source "
                    "SET error_since=COALESCE(last_checked_at, ?) "
                    "WHERE error_since IS NULL AND trim(COALESCE(last_error, ''))<>''",
                    (utc_now(),),
                )
            self._ensure_column(connection, "manga", "metadata_title", "TEXT")
            self._ensure_column(connection, "manga", "title_override", "TEXT")
            self._ensure_column(connection, "manga", "authors_override_json", "TEXT")
            self._ensure_column(
                connection,
                "source_failure",
                "transient_failures",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(connection, "manga", "status", "TEXT")
            self._ensure_column(connection, "manga", "status_override", "TEXT")
            self._ensure_column(connection, "manga", "library_status_override", "TEXT")
            for name, kind in (
                ("verified_chapter_count", "INTEGER"),
                ("verified_chapter_source", "TEXT"),
                ("verified_chapter_checked_at", "TEXT"),
            ):
                self._ensure_column(connection, "manga", name, kind)
            self._ensure_column(
                connection, "manga", "expected_count_override", "INTEGER"
            )
            # How many books the edition on disk has when it is not the
            # catalogue's (a Viz omnibus run against a tankobon map): owned
            # books then cover chapters by an even split (see chapter_mapping).
            self._ensure_column(connection, "manga", "edition_book_count", "INTEGER")
            self._ensure_column(
                connection, "manga", "expected_count_unit_override", "TEXT"
            )
            self._ensure_column(connection, "manga", "year", "INTEGER")
            self._ensure_column(connection, "manga", "last_volume", "TEXT")
            self._ensure_column(connection, "manga", "last_chapter", "TEXT")
            self._ensure_column(
                connection,
                "manga",
                "available_languages_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                connection,
                "manga",
                "external_correlations_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                connection,
                "manga",
                "creator_links_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(connection, "manga", "source_url", "TEXT")
            self._ensure_column(
                connection, "manga", "monitor_mode", "TEXT NOT NULL DEFAULT 'none'"
            )
            self._ensure_column(
                connection,
                "manga",
                "future_monitoring_allowed",
                "INTEGER NOT NULL DEFAULT 1",
            )
            self._ensure_column(
                connection,
                "manga",
                "future_monitoring_reason",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(connection, "chapter_release", "pages", "INTEGER")
            self._ensure_column(connection, "chapter_release", "version", "INTEGER")
            self._ensure_column(connection, "chapter_release", "library_sha256", "TEXT")
            self._ensure_column(
                connection, "chapter_release", "local_import_sha256", "TEXT"
            )
            self._ensure_column(
                connection,
                "deletion_operation",
                "disposition",
                "TEXT NOT NULL DEFAULT 'delete'",
            )
            self._ensure_column(
                connection, "deletion_operation", "replacement_chapter_id", "TEXT"
            )
            self._ensure_column(
                connection, "deletion_operation", "replacement_sha256", "TEXT"
            )
            self._ensure_column(
                connection,
                "deletion_operation",
                "library_paths_json",
                "TEXT NOT NULL DEFAULT '[]'",
            )
            self._ensure_column(
                connection,
                "deletion_operation",
                "komga_attempts",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self._ensure_column(
                connection, "deletion_operation", "komga_last_error", "TEXT"
            )
            self._ensure_column(
                connection, "chapter_release", "monitored", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                connection, "manga", "provider", "TEXT NOT NULL DEFAULT 'mangadex'"
            )
            self._ensure_column(connection, "manga", "source_name", "TEXT")
            self._ensure_column(connection, "manga", "source_id", "TEXT")
            self._backfill_provider_metadata_correlations(connection)
            connection.execute(
                """
                UPDATE manga
                SET cover_url = replace(cover_url, '/api/covers/', '/api/covers/mangadex/')
                WHERE cover_url LIKE '/api/covers/%'
                  AND cover_url NOT LIKE '/api/covers/mangadex/%'
                  AND cover_url NOT LIKE '/api/covers/mangapill/%'
                  AND cover_url NOT LIKE '/api/covers/local/%'
                """
            )
            self._migrate_download_job_table(connection)
            for name, kind in (
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("next_retry_at", "REAL NOT NULL DEFAULT 0"),
                ("failure_code", "TEXT NOT NULL DEFAULT ''"),
                ("failure_scope", "TEXT NOT NULL DEFAULT ''"),
            ):
                self._ensure_column(connection, "download_job", name, kind)
            if previous_version < 7:
                connection.execute(
                    "UPDATE download_job SET failure_code=substr(message, 1, instr(message, ':')-1) "
                    "WHERE status='failed' AND instr(message, ':') > 0"
                )
                for row in connection.execute(
                    "SELECT id, message FROM download_job "
                    "WHERE message LIKE 'Source unreachable (attempt %'"
                ).fetchall():
                    match = re.search(r"attempt (\d+)/", row["message"])
                    if match:
                        connection.execute(
                            "UPDATE download_job SET retry_count=?, "
                            "failure_code='ProviderUnavailableError', "
                            "failure_scope='service' WHERE id=?",
                            (int(match.group(1)), row["id"]),
                        )
            self._ensure_column(
                connection,
                "download_job",
                "origin",
                "TEXT NOT NULL DEFAULT 'automatic'",
            )
            self._ensure_column(connection, "download_job", "planned_path", "TEXT")
            self._ensure_column(connection, "torrent_download", "indexer", "TEXT")
            # Organization checks historical paths for every owned chapter;
            # the active-job partial index cannot serve all-status lookups.
            # Create this after legacy migrations that can rebuild the table.
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_job_chapter "
                "ON download_job(chapter_id)"
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_download_job_active_chapter
                ON download_job(chapter_id)
                WHERE status IN ('queued','running','downloading','packaging','importing')
                """
            )
            connection.execute(
                "UPDATE download_job SET status='queued', message='Recovered after restart' "
                "WHERE status IN ('running', 'downloading', 'packaging', 'importing')"
            )
            connection.execute(
                "UPDATE torrent_download SET status='queued', "
                "message='Recovered after restart', updated_at=? "
                "WHERE status IN ('adding', 'importing')",
                (utc_now(),),
            )
            self._retire_direct_providers(connection)
            self._reconcile_all_numbering_connection(connection)
            # Older multi-provider builds silently routed an unavailable remote
            # provider through LocalProvider. Those infrastructure failures were
            # then persisted as release-specific blocks, preventing every source
            # for the logical chapter from being retried. They carry no evidence
            # about the release itself and must not survive an upgrade.
            connection.execute(
                "DELETE FROM release_block WHERE reason IN (?, ?)",
                (
                    "Local series have no remote source",
                    "RuntimeError: Local series have no remote source",
                ),
            )
            connection.execute(
                "DELETE FROM source_failure WHERE last_reason IN (?, ?)",
                (
                    "Local series have no remote source",
                    "RuntimeError: Local series have no remote source",
                ),
            )
            connection.execute(
                "DELETE FROM source_health WHERE last_reason IN (?, ?)",
                (
                    "Local series have no remote source",
                    "RuntimeError: Local series have no remote source",
                ),
            )
            # Copy stored per series before the wording changed; the reason
            # is a stable hint, not history, so old rows adopt the new text.
            for previous in (
                "Local series have no remote source",
                "No download source linked yet; use Interactive search or Map source",
            ):
                connection.execute(
                    "UPDATE manga SET future_monitoring_reason=? "
                    "WHERE future_monitoring_reason=?",
                    ("No download source linked yet", previous),
                )
            install_revision_triggers(connection)
            if previous_version < SCHEMA_VERSION:
                connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @staticmethod
    def _retire_direct_providers(connection: sqlite3.Connection) -> None:
        """Series added through the removed MangaDex/MangaPill adapters become
        catalogue works: files and downloaded rows stay, undownloaded rows and
        mappings that could only be fetched through those adapters go."""

        retired = ("mangadex", "mangapill")
        placeholders = ",".join("?" for _ in retired)
        connection.execute(
            f"""
            UPDATE manga
            SET provider='catalogue',
                cover_url=CASE
                    WHEN cover_url LIKE '/api/covers/mangadex/%'
                      OR cover_url LIKE '/api/covers/mangapill/%'
                    THEN NULL ELSE cover_url END,
                updated_at=?
            WHERE provider IN ({placeholders})
            """,
            (utc_now(), *retired),
        )
        connection.execute(
            f"DELETE FROM manga_release_source WHERE provider IN ({placeholders})",
            retired,
        )
        active = ("queued", "running", "downloading", "packaging", "importing")
        connection.execute(
            f"""
            DELETE FROM download_job
            WHERE status NOT IN ({",".join("?" for _ in active)})
              AND chapter_id IN (
                SELECT id FROM chapter_release
                WHERE provider IN ({placeholders}) AND downloaded=0
              )
            """,
            (*active, *retired),
        )
        connection.execute(
            f"""
            DELETE FROM chapter_release
            WHERE provider IN ({placeholders}) AND downloaded=0
              AND id NOT IN (SELECT chapter_id FROM download_job)
            """,
            retired,
        )

    @staticmethod
    def _merge_provider_metadata_correlations(
        connection: sqlite3.Connection,
        *,
        manga_id: str,
        origin_provider: str,
        correlations: Iterable[dict[str, Any]],
        now: str,
    ) -> None:
        """Merge exact provider identities without deleting omitted links.

        MangaDex may occasionally return a partial ``attributes.links`` object.
        Absence is therefore not proof that a previously declared identity is
        invalid. Present sources are updated, while missing sources remain
        available until the user explicitly supersedes them with a manual pin.
        """

        for correlation in correlations:
            if not isinstance(correlation, dict):
                continue
            source = str(correlation.get("source") or "").strip().casefold()
            external_id = str(correlation.get("external_id") or "").strip()
            url = str(correlation.get("url") or "").strip()
            label = str(correlation.get("label") or source).strip()
            if not source or not external_id or not url.startswith("https://"):
                continue
            connection.execute(
                """
                INSERT INTO metadata_provider_correlation (
                    manga_id, source, external_id, url, label, origin_provider,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manga_id, source) DO UPDATE SET
                    external_id=excluded.external_id,
                    url=excluded.url,
                    label=excluded.label,
                    origin_provider=excluded.origin_provider,
                    updated_at=excluded.updated_at
                """,
                (
                    manga_id,
                    source,
                    external_id,
                    url,
                    label,
                    origin_provider,
                    now,
                    now,
                ),
            )

    @classmethod
    def _backfill_provider_metadata_correlations(
        cls, connection: sqlite3.Connection
    ) -> None:
        """Migrate exact identities captured by builds predating the table."""

        now = utc_now()
        rows = connection.execute(
            "SELECT id, provider, external_correlations_json FROM manga"
        ).fetchall()
        for row in rows:
            try:
                correlations = json.loads(row["external_correlations_json"] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(correlations, list):
                continue
            cls._merge_provider_metadata_correlations(
                connection,
                manga_id=str(row["id"]),
                origin_provider=str(row["provider"] or "mangadex"),
                correlations=correlations,
                now=now,
            )

    def upsert_manga(
        self,
        manga: dict[str, Any],
        preferred_language: str,
        monitor_mode: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO manga (
                    id, provider, title, description, cover_url, authors_json,
                    creator_links_json,
                    original_language, status, year, last_volume, last_chapter,
                    available_languages_json, external_correlations_json,
                    source_url, source_name, source_id,
                    preferred_language, monitor_mode, monitored, auto_download,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title,
                    description=excluded.description,
                    cover_url=excluded.cover_url,
                    authors_json=excluded.authors_json,
                    creator_links_json=excluded.creator_links_json,
                    original_language=excluded.original_language,
                    status=excluded.status,
                    year=excluded.year,
                    last_volume=excluded.last_volume,
                    last_chapter=excluded.last_chapter,
                    available_languages_json=excluded.available_languages_json,
                    external_correlations_json=excluded.external_correlations_json,
                    source_url=excluded.source_url,
                    source_name=excluded.source_name,
                    source_id=excluded.source_id,
                    monitor_initialized=CASE
                        WHEN manga.preferred_language <> excluded.preferred_language THEN 0
                        ELSE manga.monitor_initialized
                    END,
                    preferred_language=excluded.preferred_language,
                    monitor_mode=CASE
                        WHEN ? IS NULL THEN manga.monitor_mode
                        ELSE excluded.monitor_mode
                    END,
                    monitored=CASE
                        WHEN ? IS NULL THEN manga.monitored
                        WHEN excluded.monitor_mode IN ('all', 'future') THEN 1
                        ELSE 0
                    END,
                    auto_download=CASE
                        WHEN ? IS NULL THEN manga.auto_download
                        WHEN excluded.monitor_mode IN ('all', 'future') THEN 1
                        ELSE 0
                    END,
                    updated_at=excluded.updated_at
                """,
                (
                    manga["id"],
                    manga.get("provider", "mangadex"),
                    manga.get("source_title") or manga["title"],
                    manga.get("description", ""),
                    manga.get("cover_url"),
                    json.dumps(manga.get("authors", []), ensure_ascii=False),
                    json.dumps(manga.get("creator_links", []), ensure_ascii=False),
                    manga.get("original_language"),
                    manga.get("status"),
                    manga.get("year"),
                    manga.get("last_volume"),
                    manga.get("last_chapter"),
                    json.dumps(
                        manga.get("available_languages", []), ensure_ascii=False
                    ),
                    json.dumps(
                        manga.get("external_correlations", []), ensure_ascii=False
                    ),
                    manga.get("source_url"),
                    manga.get("source_name"),
                    manga.get("source_id"),
                    preferred_language,
                    monitor_mode or "none",
                    int((monitor_mode or "none") in {"all", "future"}),
                    int((monitor_mode or "none") in {"all", "future"}),
                    now,
                    now,
                    monitor_mode,
                    monitor_mode,
                    monitor_mode,
                ),
            )
            self._merge_provider_metadata_correlations(
                connection,
                manga_id=str(manga["id"]),
                origin_provider=str(manga.get("provider") or "mangadex"),
                correlations=manga.get("external_correlations") or [],
                now=now,
            )
        return self.get_manga(manga["id"])

    def list_manga(
        self, manga_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        ids = tuple(manga_ids) if manga_ids is not None else None
        if ids == ():
            return []
        where = (
            f"WHERE m.id IN ({','.join('?' for _ in ids)})" if ids is not None else ""
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT m.*,
                       COUNT(DISTINCT CASE WHEN c.id IS NOT NULL THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS chapter_count,
                       COUNT(DISTINCT CASE WHEN c.downloaded = 1 THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS downloaded_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NOT NULL THEN
                               COALESCE(c.volume, '') || ':' || c.chapter
                       END) AS numbered_chapter_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NULL
                            AND NULLIF(trim(c.volume), '') IS NOT NULL THEN c.volume
                       END) AS numbered_volume_count
                FROM manga m
                LEFT JOIN chapter_release c
                  ON c.manga_id = m.id AND c.language = m.preferred_language
                {where}
                GROUP BY m.id
                ORDER BY lower(COALESCE(
                    NULLIF(trim(m.title_override), ''),
                    NULLIF(trim(m.metadata_title), ''),
                    m.title
                ))
                """,
                ids or (),
            ).fetchall()
            result = [self._decode_manga(row) for row in rows]
            self._attach_logical_chapter_counts(connection, result)
        return result

    def library_revision(self) -> tuple[str, ...]:
        """Cheap fingerprint for every table used by the Library summary."""
        return self._read_model_revision(LIBRARY_REVISION_TABLES)

    def _read_model_revision(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT table_name, revision FROM read_model_revision "
                f"WHERE table_name IN ({','.join('?' for _ in tables)})",
                tables,
            ).fetchall()
        revisions = {str(row[0]): str(row[1]) for row in rows}
        return tuple(revisions.get(table, "0") for table in tables)

    def calendar_revision(self) -> tuple[str, ...]:
        """Cheap fingerprint for every row that can change the calendar."""
        return self._read_model_revision(CALENDAR_REVISION_TABLES)

    def wanted_revision(self) -> tuple[str, ...]:
        """Cheap fingerprint for the state rendered by the Wanted page."""
        return self._read_model_revision(WANTED_REVISION_TABLES)

    def manga_revision(self, manga_id: str) -> tuple[str, ...]:
        """Cheap fingerprint for every table rendered by one series page.

        Keeping this scoped to one work prevents a long-running import queue
        from invalidating the open series page whenever an unrelated chapter
        finishes.
        """

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT updated_at FROM manga WHERE id = ?) AS manga_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '')
                     FROM chapter_release WHERE manga_id = ?) AS chapter_revision,
                    (SELECT COALESCE(last_enriched_at, '') || ':' ||
                            COALESCE(last_synced_at, '') || ':' ||
                            COALESCE(last_error, '')
                     FROM series_metadata WHERE manga_id = ?) AS metadata_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '')
                     FROM volume_metadata WHERE manga_id = ?) AS volume_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '')
                     FROM series_chapter_map WHERE manga_id = ?) AS map_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '')
                     FROM volume_monitor_override WHERE manga_id = ?) AS override_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '')
                     FROM manga_release_source WHERE manga_id = ?) AS source_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(created_at), '')
                     FROM release_block WHERE manga_id = ?) AS block_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(seen_at), '')
                     FROM indexer_offer WHERE manga_id = ?) AS indexer_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(SUM(id * CASE status
                                WHEN 'queued' THEN 1
                                WHEN 'running' THEN 2
                                WHEN 'downloading' THEN 3
                                WHEN 'packaging' THEN 4
                                WHEN 'importing' THEN 5
                                ELSE 0
                            END), 0)
                     FROM download_job
                     WHERE manga_id = ? AND status IN (
                        'queued','running','downloading','packaging','importing'
                     )) AS active_job_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '')
                     FROM manga_author WHERE manga_id = ?) AS author_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(SUM(attempts), 0) || ':' ||
                            COALESCE(MAX(attempted_at), '')
                     FROM wanted_attempt WHERE manga_id = ?) AS recovery_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(fetched_at), '')
                     FROM metadata_source_record WHERE manga_id = ?) AS signals_revision,
                    (SELECT COALESCE(changed_at, '')
                     FROM publication_pause WHERE manga_id = ?) AS pause_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(measured_at), '')
                     FROM page_quality WHERE manga_id = ?) AS quality_revision,
                    (SELECT COUNT(*) || ':' || COALESCE(MAX(a.updated_at), '')
                     FROM manga_author ma JOIN author a ON a.id=ma.author_id
                     WHERE ma.manga_id = ?) AS author_identity_revision
                """,
                (manga_id,) * 16,
            ).fetchone()
        return tuple(str(value or "") for value in row)

    def get_manga(
        self, manga_id: str, *, include_logical_counts: bool = True
    ) -> dict[str, Any]:
        """One series row. ``include_logical_counts`` collapses split
        chapters into canonical coverage; skip it on hot paths that only
        need identity fields (library paths, for example)."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT m.*,
                       COUNT(DISTINCT CASE WHEN c.id IS NOT NULL THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS chapter_count,
                       COUNT(DISTINCT CASE WHEN c.downloaded = 1 THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS downloaded_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NOT NULL THEN
                               COALESCE(c.volume, '') || ':' || c.chapter
                       END) AS numbered_chapter_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NULL
                            AND NULLIF(trim(c.volume), '') IS NOT NULL THEN c.volume
                       END) AS numbered_volume_count
                FROM manga m
                LEFT JOIN chapter_release c
                  ON c.manga_id = m.id AND c.language = m.preferred_language
                WHERE m.id = ?
                GROUP BY m.id
                """,
                (manga_id,),
            ).fetchone()
            if row is None:
                raise KeyError(manga_id)
            result = self._decode_manga(row)
            if include_logical_counts:
                self._attach_logical_chapter_counts(connection, [result])
        metadata = self.get_series_metadata(manga_id)
        if metadata is not None:
            result["metadata"] = metadata["data"]
        return result

    def update_manga(self, manga_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "preferred_language",
            "monitor_mode",
            "monitored",
            "monitor_specials",
            "series_unit_override",
            "reader_mode_override",
            "reader_direction_override",
            "auto_download",
            "assemble_books_automatically",
            "status_override",
            "translation_enabled",
            "translation_source_languages",
            "library_status_override",
            "verified_chapter_count",
            "verified_chapter_source",
            "expected_count_override",
            "expected_count_unit_override",
            "edition_book_count",
        }
        selected = {key: value for key, value in updates.items() if key in allowed}
        if "translation_source_languages" in selected:
            from tankarr.translation_policy import normalize_fallback_languages

            raw = str(selected["translation_source_languages"] or "").strip()
            selected["translation_source_languages"] = (
                normalize_fallback_languages(raw) if raw else ""
            )
        if "edition_book_count" in selected:
            raw_books = selected["edition_book_count"]
            if raw_books is None or str(raw_books).strip().casefold() == "automatic":
                selected["edition_book_count"] = None
            else:
                try:
                    books = int(str(raw_books).strip())
                except (TypeError, ValueError) as exc:
                    raise ValueError(
                        "Edition book count must be a positive integer"
                    ) from exc
                if isinstance(raw_books, bool) or books < 1:
                    raise ValueError("Edition book count must be a positive integer")
                selected["edition_book_count"] = books
        if not selected:
            return self.get_manga(manga_id)
        if "status_override" in selected:
            override = str(selected["status_override"] or "").strip().casefold()
            if override == "automatic":
                selected["status_override"] = None
            elif override in {"continuing", "hiatus", "ended"}:
                selected["status_override"] = override
            else:
                raise ValueError(f"Unsupported publication status override: {override}")
        if "library_status_override" in selected:
            override = str(selected["library_status_override"] or "").strip().casefold()
            if override == "automatic":
                selected["library_status_override"] = None
            elif override == "up_to_date":
                selected["library_status_override"] = override
            else:
                raise ValueError(f"Unsupported library status override: {override}")
        if "expected_count_override" in selected:
            raw_count = selected["expected_count_override"]
            if str(raw_count or "").strip().casefold() == "automatic":
                selected["expected_count_override"] = None
                selected["expected_count_unit_override"] = None
            else:
                if isinstance(raw_count, bool):
                    raise ValueError(
                        "Expected count override must be a positive integer"
                    )
                try:
                    decimal_count = Decimal(str(raw_count))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise ValueError(
                        "Expected count override must be a positive integer"
                    ) from exc
                if (
                    not decimal_count.is_finite()
                    or decimal_count != decimal_count.to_integral_value()
                ):
                    raise ValueError(
                        "Expected count override must be a positive integer"
                    )
                count = int(decimal_count)
                if count < 1 or count > 100_000:
                    raise ValueError(
                        "Expected count override must be between 1 and 100000"
                    )
                unit = (
                    str(selected.get("expected_count_unit_override") or "")
                    .strip()
                    .casefold()
                )
                if unit not in {"chapter", "volume", "issue", "book"}:
                    raise ValueError(
                        "Expected count unit override must identify the current library unit"
                    )
                selected["expected_count_override"] = count
                selected["expected_count_unit_override"] = unit
        elif "expected_count_unit_override" in selected:
            raise ValueError("Expected count unit cannot be changed on its own")
        if "verified_chapter_count" in selected:
            raw = selected["verified_chapter_count"]
            if raw == "automatic":
                selected["verified_chapter_count"] = None
                selected["verified_chapter_source"] = None
                selected["verified_chapter_checked_at"] = None
            else:
                if (
                    isinstance(raw, bool)
                    or not isinstance(raw, int)
                    or not 1 <= raw <= 100_000
                ):
                    raise ValueError(
                        "Verified chapter number must be between 1 and 100000"
                    )
                source = str(selected.get("verified_chapter_source") or "").strip()
                if not source or len(source) > 2000:
                    raise ValueError(
                        "A source is required for the verified chapter number"
                    )
                selected["verified_chapter_source"] = source
                selected["verified_chapter_checked_at"] = utc_now()
        elif "verified_chapter_source" in selected:
            raise ValueError("Verified chapter source requires a chapter number")
        selected["updated_at"] = utc_now()
        current = self.get_manga(manga_id)
        if (
            "preferred_language" in selected
            and selected["preferred_language"] != current["preferred_language"]
        ):
            selected["monitor_initialized"] = 0
            selected["last_check_error"] = None
        if "monitor_mode" in selected:
            selected["monitored"] = int(selected["monitor_mode"] in {"all", "future"})
            selected["auto_download"] = int(
                selected["monitor_mode"] in {"all", "future"}
            )
        assignments = ", ".join(f"{key}=?" for key in selected)
        values = list(selected.values()) + [manga_id]
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE manga SET {assignments} WHERE id=?", values
            )
            if cursor.rowcount == 0:
                raise KeyError(manga_id)
        return self.get_manga(manga_id)

    def set_manga_title_override(
        self, manga_id: str, title_override: str | None
    ) -> dict[str, Any]:
        """Persist a user-facing title without replacing provider identity data."""

        normalized = str(title_override or "").strip() or None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE manga
                SET title_override=?, updated_at=?
                WHERE id=?
                """,
                (normalized, utc_now(), manga_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(manga_id)
        return self.get_manga(manga_id)

    def set_manga_authors_override(
        self, manga_id: str, authors: list[str] | None
    ) -> dict[str, Any]:
        """Persist hand-corrected creators; catalogue data stays untouched.

        Sources credit an illustrator as the author, or miss a co-author
        entirely. The override is the operator's answer and outlives every
        metadata refresh.
        """

        cleaned = [str(name).strip() for name in authors or [] if str(name).strip()]
        payload = json.dumps(cleaned, ensure_ascii=False) if cleaned else None
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE manga SET authors_override_json=?, updated_at=? WHERE id=?",
                (payload, utc_now(), manga_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(manga_id)
        return self.get_manga(manga_id)

    def set_manga_metadata_title(
        self, manga_id: str, metadata_title: str | None
    ) -> dict[str, Any]:
        """Persist the verified automatic title separately from provider identity."""

        normalized = str(metadata_title or "").strip() or None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE manga
                SET metadata_title=?, updated_at=?
                WHERE id=?
                """,
                (normalized, utc_now(), manga_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(manga_id)
        return self.get_manga(manga_id)

    def set_manga_title_state(
        self,
        manga_id: str,
        *,
        metadata_title: str | None,
        title_override: str | None,
    ) -> dict[str, Any]:
        """Atomically update both automatic and user-controlled title layers."""

        normalized_metadata = str(metadata_title or "").strip() or None
        normalized_override = str(title_override or "").strip() or None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE manga
                SET metadata_title=?, title_override=?, updated_at=?
                WHERE id=?
                """,
                (normalized_metadata, normalized_override, utc_now(), manga_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(manga_id)
        return self.get_manga(manga_id)

    def set_future_monitoring_capability(
        self,
        manga_id: str,
        *,
        allowed: bool,
        reason: str,
        monitor_mode: str | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {
            "future_monitoring_allowed": int(allowed),
            "future_monitoring_reason": reason[:1000],
            "updated_at": utc_now(),
        }
        if monitor_mode is not None:
            updates["monitor_mode"] = monitor_mode
            updates["monitored"] = int(monitor_mode in {"all", "future"})
            updates["auto_download"] = int(monitor_mode in {"all", "future"})
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE manga SET {assignments} WHERE id=?",
                [*updates.values(), manga_id],
            )
            if cursor.rowcount == 0:
                raise KeyError(manga_id)
        return self.get_manga(manga_id)

    def upsert_chapters(
        self,
        manga_id: str,
        chapters: Iterable[dict[str, Any]],
        *,
        monitor_new: bool = True,
    ) -> dict[str, Any]:
        now = utc_now()
        chapter_list = list(chapters)
        new_ids: list[str] = []
        with self.connect() as connection:
            requested_ids = {
                str(chapter["id"])
                for chapter in chapter_list
                if str(chapter.get("id") or "")
            }
            if requested_ids:
                placeholders = ",".join("?" for _ in requested_ids)
                conflicting = connection.execute(
                    f"""
                    SELECT id, manga_id, provider
                    FROM chapter_release
                    WHERE id IN ({placeholders}) AND manga_id<>?
                    LIMIT 1
                    """,
                    [*requested_ids, manga_id],
                ).fetchone()
                if conflicting is not None:
                    raise ValueError(
                        "A provider release is already assigned to another series: "
                        f"{conflicting['provider']}:{conflicting['id']}"
                    )
            existing_ids = {
                str(row["id"])
                for row in connection.execute(
                    "SELECT id FROM chapter_release WHERE manga_id=?", (manga_id,)
                ).fetchall()
            }
            for chapter in chapter_list:
                if chapter["id"] not in existing_ids:
                    new_ids.append(chapter["id"])
                    existing_ids.add(chapter["id"])
                source_chapter = chapter.get("source_chapter", chapter.get("chapter"))
                canonical_chapter = chapter.get(
                    "canonical_chapter", chapter.get("chapter")
                )
                numbering_status = str(
                    chapter.get("numbering_status")
                    or ("mapped" if canonical_chapter is not None else "unmapped")
                )
                compatibility_chapter = (
                    canonical_chapter if numbering_status == "mapped" else None
                )
                connection.execute(
                    """
                    INSERT INTO chapter_release (
                        id, manga_id, volume, chapter, source_chapter,
                        canonical_chapter, numbering_status, numbering_method,
                        numbering_confidence, numbering_evidence_json,
                        numbering_version, title, language, provider,
                        groups_json, publish_at, source_url, pages, version,
                        monitored, first_seen_at, updated_at, source_key, source_name
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        -- A release reclassified as a whole volume keeps that
                        -- unit: the provider's "chapter" number is its volume.
                        -- A source that names no volume does not erase the
                        -- book this chapter was filed under: most catalogues
                        -- never name one, so every refresh used to wipe the
                        -- number the book map had just written, the organizer
                        -- renamed the file back, and the reader collected a
                        -- dead record for each round trip.
                        volume=CASE
                            WHEN chapter_release.release_unit='volume'
                            THEN COALESCE(excluded.chapter, excluded.volume)
                            ELSE COALESCE(excluded.volume, chapter_release.volume) END,
                        chapter=CASE
                            WHEN chapter_release.release_unit='volume' THEN NULL
                            ELSE excluded.chapter END,
                        source_chapter=excluded.source_chapter,
                        canonical_chapter=CASE
                            WHEN chapter_release.release_unit='volume' THEN NULL
                            ELSE excluded.canonical_chapter END,
                        numbering_status=excluded.numbering_status,
                        numbering_method=excluded.numbering_method,
                        numbering_confidence=excluded.numbering_confidence,
                        numbering_evidence_json=excluded.numbering_evidence_json,
                        numbering_version=excluded.numbering_version,
                        title=excluded.title,
                        language=excluded.language,
                        groups_json=excluded.groups_json,
                        publish_at=excluded.publish_at,
                        source_url=excluded.source_url,
                        -- A provider can retile a webtoon without changing
                        -- the file already downloaded. Its remote image count
                        -- must not overwrite the measured local page count.
                        pages=CASE
                            WHEN chapter_release.downloaded=1 AND chapter_release.pages>0
                            THEN chapter_release.pages ELSE excluded.pages END,
                        version=excluded.version,
                        updated_at=excluded.updated_at,
                        source_key=COALESCE(excluded.source_key, chapter_release.source_key),
                        source_name=COALESCE(excluded.source_name, chapter_release.source_name)
                    """,
                    (
                        chapter["id"],
                        manga_id,
                        chapter.get("volume"),
                        compatibility_chapter,
                        source_chapter,
                        canonical_chapter,
                        numbering_status,
                        str(chapter.get("numbering_method") or "source_identity"),
                        float(chapter.get("numbering_confidence") or 0.0),
                        json.dumps(
                            chapter.get("numbering_evidence") or {},
                            ensure_ascii=False,
                        ),
                        int(chapter.get("numbering_version") or 0),
                        chapter.get("title", ""),
                        chapter["language"],
                        chapter["provider"],
                        json.dumps(chapter.get("groups", []), ensure_ascii=False),
                        chapter.get("publish_at"),
                        chapter["source_url"],
                        chapter.get("pages"),
                        chapter.get("version"),
                        int(monitor_new),
                        now,
                        now,
                        chapter.get("source_key") or chapter["provider"],
                        chapter.get("source_name"),
                    ),
                )
            self._reconcile_numbering_connection(connection, manga_id)
        return {"seen": len(chapter_list), "new_chapter_ids": new_ids}

    @staticmethod
    def _numbering_source_roles(
        connection: sqlite3.Connection, manga_id: str
    ) -> dict[str, str]:
        persisted = connection.execute(
            "SELECT host, source_role FROM manga_official_source WHERE manga_id=?",
            (manga_id,),
        ).fetchall()
        if persisted:
            # A host can expose several editions. The source used for the
            # managed language wins over secondary evidence, which wins over
            # unrelated editions on that same host.
            priority = {
                "alternative": 0,
                "secondary_official": 1,
                "primary_official": 2,
            }
            roles: dict[str, str] = {}
            for item in persisted:
                host = str(item["host"] or "").casefold()
                role = str(item["source_role"] or "alternative")
                if host and priority.get(role, -1) >= priority.get(
                    roles.get(host, ""), -1
                ):
                    roles[host] = role
            return roles
        row = connection.execute(
            """
            SELECT m.preferred_language, m.original_language, sm.data_json
            FROM manga AS m
            LEFT JOIN series_metadata AS sm ON sm.manga_id=m.id
            WHERE m.id=?
            """,
            (manga_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            metadata = json.loads(row["data_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        return official_source_roles(
            metadata.get("official_links"),
            preferred_language=str(row["preferred_language"] or ""),
            original_language=str(row["original_language"] or ""),
        )

    @staticmethod
    def _refresh_official_edition_sources_connection(
        connection: sqlite3.Connection, manga_id: str
    ) -> dict[str, str]:
        """Persist editorial editions, including metadata-only publishers."""

        row = connection.execute(
            """
            SELECT m.preferred_language, m.original_language, sm.data_json
            FROM manga AS m
            LEFT JOIN series_metadata AS sm ON sm.manga_id=m.id
            WHERE m.id=?
            """,
            (manga_id,),
        ).fetchone()
        if row is None:
            return {}
        try:
            metadata = json.loads(row["data_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            metadata = {}
        roles = official_source_roles(
            metadata.get("official_links"),
            preferred_language=str(row["preferred_language"] or ""),
            original_language=str(row["original_language"] or ""),
        )
        connection.execute(
            "DELETE FROM manga_official_source WHERE manga_id=?", (manga_id,)
        )
        now = utc_now()
        links = metadata.get("official_links")
        if not isinstance(links, list):
            return roles
        for link in links:
            if not isinstance(link, dict):
                continue
            source_url = str(link.get("url") or "").strip()
            host = release_host({"source_url": source_url})
            if not source_url or not host:
                continue
            language = str(link.get("language") or "").casefold().split("-", 1)[0]
            preferred_language = str(row["preferred_language"] or "").casefold()
            original_language = str(row["original_language"] or "").casefold()
            if language and language == preferred_language:
                role = "primary_official"
            elif language and language == original_language:
                role = "secondary_official"
            else:
                # Some MangaBaka links omit their locale. Retain the host's
                # discovered role for those, but never promote another known
                # edition merely because it shares the same publisher host.
                role = roles.get(host, "alternative") if not language else "alternative"
            connection.execute(
                """
                INSERT INTO manga_official_source (
                    manga_id, host, language, source_url, source_role, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    manga_id,
                    host,
                    language,
                    source_url,
                    role,
                    now,
                ),
            )
        return roles

    @staticmethod
    def _refresh_release_source_roles_connection(
        connection: sqlite3.Connection, manga_id: str, source_roles: dict[str, str]
    ) -> None:
        """Persist edition roles for mapped sources without guessing by name."""

        rows = connection.execute(
            "SELECT provider, provider_manga_id, source_url FROM manga_release_source "
            "WHERE manga_id=?",
            (manga_id,),
        ).fetchall()
        for row in rows:
            host = release_host({"source_url": row["source_url"]})
            role = "alternative"
            for candidate, candidate_role in source_roles.items():
                if host and (host == candidate or host.endswith("." + candidate)):
                    role = candidate_role
                    break
            connection.execute(
                "UPDATE manga_release_source SET source_role=? "
                "WHERE manga_id=? AND provider=? AND provider_manga_id=?",
                (role, manga_id, row["provider"], row["provider_manga_id"]),
            )

    @staticmethod
    def _secondary_official_evidence_connection(
        connection: sqlite3.Connection, manga_id: str, source_roles: dict[str, str]
    ) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT host, provider_index, edition_chapter, title, publish_at, "
            "source_url, evidence_json FROM official_edition_evidence "
            "WHERE manga_id=?",
            (manga_id,),
        ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            host = str(row["host"] or "").casefold()
            if source_roles.get(host) != "secondary_official":
                continue
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = {}
            output.append(
                {
                    "host": host,
                    "source_chapter": row["provider_index"],
                    "edition_chapter": row["edition_chapter"],
                    "title": row["title"],
                    "publish_at": row["publish_at"],
                    "source_url": row["source_url"],
                    "evidence": evidence,
                }
            )
        return output

    def replace_official_edition_evidence(
        self,
        manga_id: str,
        *,
        host: str,
        language: str,
        items: Iterable[dict[str, Any]],
    ) -> int:
        """Replace one publisher's read-only edition index and reconcile."""

        normalized_host = str(host).casefold().removeprefix("www.")
        if not normalized_host:
            raise ValueError("Official evidence host is required")
        now = utc_now()
        rows = [dict(item) for item in items if str(item.get("provider_index") or "")]
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                "DELETE FROM official_edition_evidence WHERE manga_id=? AND host=?",
                (manga_id, normalized_host),
            )
            for item in rows:
                connection.execute(
                    """
                    INSERT INTO official_edition_evidence (
                        manga_id, host, edition_language, provider_index,
                        edition_chapter, title, publish_at, source_url,
                        evidence_json, checked_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manga_id,
                        normalized_host,
                        str(language or "unknown").casefold(),
                        str(item["provider_index"]),
                        canonical_number(item.get("edition_chapter")),
                        str(item.get("title") or ""),
                        item.get("publish_at"),
                        str(item.get("source_url") or ""),
                        json.dumps(item.get("evidence") or {}, ensure_ascii=False),
                        now,
                    ),
                )
            self._reconcile_numbering_connection(connection, manga_id)
        return len(rows)

    def official_edition_evidence_due(
        self, manga_id: str, host: str, *, max_age_seconds: float
    ) -> bool:
        """Whether a publisher index needs a new read-only metadata probe."""

        with self.connect() as connection:
            row = connection.execute(
                "SELECT MAX(checked_at) AS checked_at FROM official_edition_evidence "
                "WHERE manga_id=? AND host=?",
                (manga_id, str(host).casefold().removeprefix("www.")),
            ).fetchone()
        raw = str((row or {})["checked_at"] or "")
        try:
            checked = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        if checked.tzinfo is None:
            checked = checked.replace(tzinfo=UTC)
        return (datetime.now(UTC) - checked).total_seconds() >= max_age_seconds

    def replace_numbering_overrides(
        self, manga_id: str, assignments: dict[str, str], *, evidence: str
    ) -> None:
        """Record reviewed correspondences between a source and this edition.

        Release IDs scope the decision to one source. The observed source number
        is retained so a future provider renumbering cannot reuse stale evidence.
        An empty mapping withdraws the series' previous correspondences.
        """
        from tankarr.chapter_map import canonical_label

        if assignments and not evidence.strip():
            raise ValueError("Numbering correspondences require evidence")
        with self.connect() as connection:
            if not connection.execute(
                "SELECT 1 FROM manga WHERE id=?", (manga_id,)
            ).fetchone():
                raise KeyError(manga_id)
            records = []
            for release_id, number in assignments.items():
                canonical = canonical_label(number)
                row = connection.execute(
                    "SELECT * FROM chapter_release WHERE id=? AND manga_id=?",
                    (release_id, manga_id),
                ).fetchone()
                source = canonical_label(row["source_chapter"]) if row else None
                if canonical is None or source is None or is_volume_release(dict(row)):
                    raise ValueError("Expected a chapter release and numeric chapter")
                records.append(
                    (
                        manga_id,
                        release_id,
                        source,
                        canonical,
                        evidence.strip(),
                        utc_now(),
                    )
                )
            connection.execute(
                "DELETE FROM release_numbering_override WHERE manga_id=?", (manga_id,)
            )
            connection.executemany(
                "INSERT INTO release_numbering_override VALUES (?, ?, ?, ?, ?, ?)",
                records,
            )
            self._reconcile_numbering_connection(connection, manga_id)

    def _reconcile_numbering_connection(
        self, connection: sqlite3.Connection, manga_id: str
    ) -> dict[str, int]:
        rows = connection.execute(
            "SELECT * FROM chapter_release WHERE manga_id=?",
            (manga_id,),
        ).fetchall()
        releases = [self._decode_chapter(row) for row in rows]
        persisted_releases = {str(release["id"]): dict(release) for release in releases}
        # Rebuild withdrawn or invalidated overrides from the source, not the
        # previously corrected number. Active overrides are reapplied below.
        for release in releases:
            if release.get("numbering_method") == "operator_correspondence":
                release.update(
                    canonical_chapter=release.get("source_chapter"),
                    chapter=release.get("source_chapter"),
                    numbering_method="source_identity",
                    numbering_confidence=0.6,
                    numbering_evidence={},
                )
        source_roles = self._numbering_source_roles(connection, manga_id)
        self._refresh_release_source_roles_connection(
            connection, manga_id, source_roles
        )
        from tankarr.chapter_mapping import (
            canonical_decimal_labels,
        )

        manga_row = connection.execute(
            "SELECT * FROM manga WHERE id=?", (manga_id,)
        ).fetchone()
        metadata_row = connection.execute(
            "SELECT data_json FROM series_metadata WHERE manga_id=?", (manga_id,)
        ).fetchone()
        metadata = json.loads(metadata_row["data_json"]) if metadata_row else {}

        from tankarr.series_form import _is_webtoon

        webtoon = _is_webtoon(metadata)

        chapter_total = (
            counted_chapter_total(dict(manga_row), metadata) if manga_row else None
        )
        map_entries = self._chapter_map_connection(connection, manga_id)
        # A webtoon's seasons are dropped from the effective map, so the claim
        # has to be read raw: only a book some source actually claims can have
        # its membership rejected here.
        claimed_books = {
            canonical_number(volume)
            for entry in self._chapter_map_connection(connection, manga_id, raw=True)
            if entry.source not in {"operator", "ocr"}
            for volume in entry.volumes
        }
        verified_volumes: dict[str, set[str]] = defaultdict(set)
        reviewed_books: set[str] = set()
        for entry in map_entries:
            if (
                entry.exact
                and entry.source in {"operator", "ocr"}
                and len(entry.volumes) == 1
            ):
                reviewed_books.add(entry.volumes[0])
                for label in entry.chapters:
                    verified_volumes[label].add(entry.volumes[0])
        decisions = reconcile_numbering(
            releases,
            source_roles=source_roles,
            secondary_evidence=self._secondary_official_evidence_connection(
                connection, manga_id, source_roles
            ),
            canonical_labels=canonical_decimal_labels(
                map_entries,
                releases,
                chapter_total=chapter_total,
            ),
            canonical_end=chapter_total,
        )
        for override in connection.execute(
            "SELECT * FROM release_numbering_override WHERE manga_id=?", (manga_id,)
        ):
            decision = decisions.get(override["release_id"])
            if (
                decision is None
                or canonical_number(decision.get("source_chapter"))
                != override["source_chapter"]
            ):
                continue
            decision.update(
                canonical_chapter=override["canonical_chapter"],
                numbering_status="mapped",
                numbering_method="operator_correspondence",
                numbering_confidence=1.0,
                numbering_evidence={
                    "source_chapter": override["source_chapter"],
                    "evidence": override["evidence"],
                },
            )
        counts: dict[str, int] = defaultdict(int)
        releases_by_id = persisted_releases
        reconciled_at = utc_now()
        for decision in decisions.values():
            status = str(decision["numbering_status"])
            canonical = decision.get("canonical_chapter")
            compatibility = canonical if status == "mapped" else None
            evidence = decision.get("numbering_evidence") or {}
            existing = releases_by_id[str(decision["id"])]
            # Resolve membership after source numbering. A refresh must not
            # replace a verified book assignment with the provider's old tag.
            owners = verified_volumes.get(canonical_number(compatibility) or "", set())
            volume = (
                next(iter(owners))
                if len(owners) == 1 and not is_volume_release(existing)
                else existing.get("volume")
            )
            if (
                not is_volume_release(existing)
                and len(owners) != 1
                and (
                    canonical_number(volume) in reviewed_books
                    or (webtoon and canonical_number(volume) in claimed_books)
                )
            ):
                # An explicit review also rejects the source's old membership.
                # Empty reviewed books intentionally have unknown boundaries;
                # refreshing the source must not restore its rejected tags.
                volume = None
            changed = any(
                (
                    volume != existing.get("volume"),
                    decision.get("source_chapter") != existing.get("source_chapter"),
                    decision.get("edition_chapter") != existing.get("edition_chapter"),
                    canonical != existing.get("canonical_chapter"),
                    decision.get("primary_chapter") != existing.get("primary_chapter"),
                    compatibility != existing.get("chapter"),
                    status != str(existing.get("numbering_status") or "mapped"),
                    str(decision["numbering_method"])
                    != str(existing.get("numbering_method") or "legacy"),
                    float(decision["numbering_confidence"])
                    != float(existing.get("numbering_confidence") or 0.0),
                    evidence != (existing.get("numbering_evidence") or {}),
                    int(decision["numbering_version"])
                    != int(existing.get("numbering_version") or 0),
                )
            )
            connection.execute(
                """
                UPDATE chapter_release
                SET volume=?, source_chapter=?, edition_chapter=?, canonical_chapter=?,
                    primary_chapter=?, chapter=?,
                    numbering_status=?, numbering_method=?,
                    numbering_confidence=?, numbering_evidence_json=?,
                    numbering_version=?, updated_at=?
                WHERE id=?
                """,
                (
                    volume,
                    decision.get("source_chapter"),
                    decision.get("edition_chapter"),
                    canonical,
                    decision.get("primary_chapter"),
                    compatibility,
                    status,
                    decision["numbering_method"],
                    decision["numbering_confidence"],
                    json.dumps(evidence, ensure_ascii=False),
                    decision["numbering_version"],
                    reconciled_at if changed else existing.get("updated_at"),
                    decision["id"],
                ),
            )
            counts[status] += 1
        return dict(counts)

    def _reconcile_all_numbering_connection(
        self, connection: sqlite3.Connection
    ) -> dict[str, int]:
        manga_ids = [
            str(row["manga_id"])
            for row in connection.execute(
                "SELECT DISTINCT manga_id FROM chapter_release"
            ).fetchall()
        ]
        totals: dict[str, int] = defaultdict(int)
        for manga_id in manga_ids:
            for status, count in self._reconcile_numbering_connection(
                connection, manga_id
            ).items():
                totals[status] += count
        return dict(totals)

    def reconcile_all_numbering(self) -> dict[str, int]:
        """Recompute canonical identities without touching library files."""

        with self.connect() as connection:
            return self._reconcile_all_numbering_connection(connection)

    def shadow_numbering(self, manga_ids: Iterable[str]) -> list[dict[str, Any]]:
        """Preview a reconciliation without changing releases or library files."""

        reports: list[dict[str, Any]] = []
        with self.connect() as connection:
            for manga_id in dict.fromkeys(str(value) for value in manga_ids):
                manga = connection.execute(
                    "SELECT * FROM manga WHERE id=?", (manga_id,)
                ).fetchone()
                if manga is None:
                    continue
                releases = [
                    self._decode_chapter(row)
                    for row in connection.execute(
                        "SELECT * FROM chapter_release WHERE manga_id=?", (manga_id,)
                    ).fetchall()
                ]
                source_roles = self._numbering_source_roles(connection, manga_id)
                metadata_row = connection.execute(
                    "SELECT data_json FROM series_metadata WHERE manga_id=?",
                    (manga_id,),
                ).fetchone()
                metadata = json.loads(metadata_row["data_json"]) if metadata_row else {}
                decisions = reconcile_numbering(
                    releases,
                    source_roles=source_roles,
                    secondary_evidence=self._secondary_official_evidence_connection(
                        connection,
                        manga_id,
                        source_roles,
                    ),
                    canonical_end=counted_chapter_total(dict(manga), metadata),
                )
                status_counts: dict[str, int] = defaultdict(int)
                method_counts: dict[str, int] = defaultdict(int)
                changes = 0
                mapped_primary: list[int] = []
                for release in releases:
                    decision = decisions.get(str(release["id"]))
                    if decision is None:
                        continue
                    status_counts[str(decision["numbering_status"])] += 1
                    method_counts[str(decision["numbering_method"])] += 1
                    if decision.get("canonical_chapter") != release.get(
                        "canonical_chapter"
                    ) or str(decision["numbering_status"]) != str(
                        release.get("numbering_status") or "mapped"
                    ):
                        changes += 1
                    number = canonical_number(decision.get("primary_chapter"))
                    if number is not None:
                        try:
                            value = int(Decimal(number))
                            if value > 0:
                                mapped_primary.append(value)
                        except (InvalidOperation, ValueError):
                            pass
                reports.append(
                    {
                        "manga_id": manga_id,
                        "title": str(manga["title"]),
                        "source_roles": source_roles,
                        "releases": len(releases),
                        "changes": changes,
                        "statuses": dict(sorted(status_counts.items())),
                        "methods": dict(sorted(method_counts.items())),
                        "primary_range": (
                            [min(mapped_primary), max(mapped_primary)]
                            if mapped_primary
                            else None
                        ),
                    }
                )
        return reports

    def upsert_release_source(
        self,
        manga_id: str,
        *,
        provider: str,
        provider_manga_id: str,
        title: str,
        source_url: str | None,
        source_name: str | None,
        language: str,
        match_confidence: float,
        match_reason: str,
        verified_by: str = "automatic",
    ) -> dict[str, Any]:
        """Persist a verified download-source identity for one canonical work.

        One entry on a download source is one work: mapping it to a second
        series always breaks one of them, because a chapter can only belong
        to a single series (see the ownership guard in ``upsert_chapters``).
        The loser would then fail on every sync forever, so the second claim
        is refused here and the caller surfaces it as a decision instead.
        """

        self.get_manga(manga_id)
        now = utc_now()
        with self.connect() as connection:
            owner = connection.execute(
                """
                SELECT source.manga_id AS manga_id,
                       COALESCE(
                           NULLIF(trim(manga.title_override), ''),
                           NULLIF(trim(manga.metadata_title), ''),
                           manga.title
                       ) AS title
                FROM manga_release_source AS source
                LEFT JOIN manga ON manga.id = source.manga_id
                WHERE source.provider=? AND source.provider_manga_id=?
                  AND source.manga_id<>?
                LIMIT 1
                """,
                (provider, provider_manga_id, manga_id),
            ).fetchone()
            if owner is not None:
                raise ReleaseSourceOwned(
                    f"{provider}:{provider_manga_id} is already the download "
                    f"source of another series ({owner['manga_id']})",
                    owner_id=str(owner["manga_id"]),
                    owner_title=str(owner["title"] or ""),
                )
            connection.execute(
                """
                INSERT INTO manga_release_source (
                    manga_id, provider, provider_manga_id, title, source_url,
                    source_name, language, match_confidence, match_reason,
                    verified_by, enabled, last_checked_at, last_error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, NULL, ?, ?)
                ON CONFLICT(manga_id, provider, provider_manga_id) DO UPDATE SET
                    title=excluded.title,
                    source_url=excluded.source_url,
                    source_name=excluded.source_name,
                    language=excluded.language,
                    match_confidence=excluded.match_confidence,
                    match_reason=excluded.match_reason,
                    verified_by=CASE
                        WHEN manga_release_source.verified_by='manual' THEN 'manual'
                        ELSE excluded.verified_by
                    END,
                    enabled=1,
                    updated_at=excluded.updated_at
                """,
                (
                    manga_id,
                    provider,
                    provider_manga_id,
                    title,
                    source_url,
                    source_name,
                    language,
                    max(0.0, min(1.0, float(match_confidence))),
                    str(match_reason)[:1000],
                    verified_by,
                    now,
                    now,
                    now,
                ),
            )
        return self.get_release_source(manga_id, provider, provider_manga_id)

    def get_release_source(
        self, manga_id: str, provider: str, provider_manga_id: str
    ) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM manga_release_source
                WHERE manga_id=? AND provider=? AND provider_manga_id=?
                """,
                (manga_id, provider, provider_manga_id),
            ).fetchone()
        if row is None:
            raise KeyError((manga_id, provider, provider_manga_id))
        return self._decode_release_source(row)

    def forget_undownloaded_releases(
        self, manga_id: str, release_ids: list[str]
    ) -> int:
        """Drop the given releases of one series unless a file came from them."""

        ids = [str(item) for item in release_ids]
        if not ids:
            return 0
        removed = 0
        with self.connect() as connection:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                cursor = connection.execute(
                    "DELETE FROM chapter_release WHERE manga_id=? AND downloaded=0 "
                    f"AND id IN ({','.join('?' * len(chunk))})",
                    (manga_id, *chunk),
                )
                removed += int(cursor.rowcount or 0)
        return removed

    def forget_source_releases(
        self,
        manga_id: str,
        *,
        provider: str,
        source_name: str | None,
        release_ids: list[str] | None = None,
    ) -> int:
        """Drop every undownloaded release one source entry gave a series.

        Matching by the source itself (provider and source name) rather than
        by what it lists *now*: a source that is gone, broken or in timeout
        lists nothing, and its stale rows used to survive the unmapping
        (Niadd left 30 000 "doujin" rows behind). Explicit ids are dropped
        too, for providers whose rows carry no source name.
        """

        removed = self.forget_undownloaded_releases(manga_id, list(release_ids or []))
        with self.connect() as connection:
            if source_name:
                cursor = connection.execute(
                    "DELETE FROM chapter_release WHERE manga_id=? AND downloaded=0 "
                    "AND provider=? AND lower(COALESCE(source_name,''))=lower(?)",
                    (manga_id, provider, source_name),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM chapter_release WHERE manga_id=? AND downloaded=0 "
                    "AND provider=? AND (source_key IS NULL OR source_key=? OR source_key='')",
                    (manga_id, provider, provider),
                )
            removed += int(cursor.rowcount or 0)
        return removed

    def add_release_source_rejection(
        self, manga_id: str, provider: str, provider_manga_id: str, reason: str
    ) -> None:
        """Remember that this source entry must not be mapped to this series
        again on its own: the operator unmapped it, or its list was refused."""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO release_source_rejection
                    (manga_id, provider, provider_manga_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(manga_id, provider, provider_manga_id) DO UPDATE SET
                    reason=excluded.reason, created_at=excluded.created_at
                """,
                (
                    manga_id,
                    provider,
                    str(provider_manga_id),
                    str(reason)[:500],
                    utc_now(),
                ),
            )

    def clear_release_source_rejection(
        self, manga_id: str, provider: str, provider_manga_id: str
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM release_source_rejection "
                "WHERE manga_id=? AND provider=? AND provider_manga_id=?",
                (manga_id, provider, str(provider_manga_id)),
            )
        return int(cursor.rowcount or 0)

    def release_source_rejections(self, manga_id: str) -> dict[tuple[str, str], str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT provider, provider_manga_id, reason FROM release_source_rejection "
                "WHERE manga_id=?",
                (manga_id,),
            ).fetchall()
        return {
            (str(row["provider"]), str(row["provider_manga_id"])): str(row["reason"])
            for row in rows
        }

    def delete_release_source(
        self, manga_id: str, provider: str, provider_manga_id: str
    ) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM manga_release_source
                WHERE manga_id=? AND provider=? AND provider_manga_id=?
                """,
                (manga_id, provider, provider_manga_id),
            )
        return cursor.rowcount

    def retire_uninstalled_sources(
        self, installed_source_keys: set[str]
    ) -> dict[str, int]:
        """Forget what a source that is no longer installed was offering.

        Its undownloaded releases would otherwise stay download candidates
        and fail with 'missing source' one by one; the files it already
        delivered stay, they are the library's now.
        """

        keys = {str(key).casefold() for key in installed_source_keys}
        if not keys:
            return {"releases": 0, "mappings": 0}
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT source_key, source_name FROM chapter_release "
                "WHERE provider='suwayomi' AND source_key IS NOT NULL"
            ).fetchall()
            gone = [
                (str(row["source_key"]), str(row["source_name"] or ""))
                for row in rows
                if str(row["source_key"]).casefold() not in keys
            ]
            releases = mappings = 0
            for key, source_name in gone:
                releases += connection.execute(
                    "DELETE FROM chapter_release WHERE source_key=? AND downloaded=0",
                    (key,),
                ).rowcount
                if source_name:
                    mappings += connection.execute(
                        "DELETE FROM manga_release_source "
                        "WHERE provider='suwayomi' AND source_name=?",
                        (source_name,),
                    ).rowcount
        return {"releases": releases, "mappings": mappings}

    def retire_stale_provider_identities(
        self, provider: str, is_current: Callable[[str], bool]
    ) -> dict[str, int]:
        """Drop mappings and undownloaded releases whose ids no longer resolve.

        Downloaded releases are kept: their files exist and their history
        matters. Releases with an active job are left for the worker.
        """

        active = ("queued", "running", "downloading", "packaging", "importing")
        with self.connect() as connection:
            mappings = connection.execute(
                "SELECT manga_id, provider_manga_id FROM manga_release_source "
                "WHERE provider=?",
                (provider,),
            ).fetchall()
            stale_mappings = [
                (row["manga_id"], row["provider_manga_id"])
                for row in mappings
                if not is_current(str(row["provider_manga_id"]))
            ]
            for manga_id, provider_manga_id in stale_mappings:
                connection.execute(
                    "DELETE FROM manga_release_source WHERE manga_id=? AND provider=? "
                    "AND provider_manga_id=?",
                    (manga_id, provider, provider_manga_id),
                )
            rows = connection.execute(
                "SELECT id FROM chapter_release WHERE provider=? AND downloaded=0",
                (provider,),
            ).fetchall()
            stale_ids = [
                str(row["id"]) for row in rows if not is_current(str(row["id"]))
            ]
            removed = 0
            jobs_deleted = 0
            for chapter_id in stale_ids:
                busy = connection.execute(
                    f"SELECT 1 FROM download_job WHERE chapter_id=? AND status IN "
                    f"({','.join('?' for _ in active)}) LIMIT 1",
                    (chapter_id, *active),
                ).fetchone()
                if busy:
                    continue
                jobs_deleted += connection.execute(
                    "DELETE FROM download_job WHERE chapter_id=?", (chapter_id,)
                ).rowcount
                connection.execute(
                    "DELETE FROM release_block WHERE chapter_id=?", (chapter_id,)
                )
                removed += connection.execute(
                    "DELETE FROM chapter_release WHERE id=?", (chapter_id,)
                ).rowcount
        return {
            "mappings_removed": len(stale_mappings),
            "releases_removed": removed,
            "jobs_removed": jobs_deleted,
        }

    def adopt_catalogue_identity(
        self,
        manga_id: str,
        external_id: str,
        *,
        source_label: str = "MangaBaka",
        source_url: str | None = None,
    ) -> bool:
        """Turn a local-import series into a catalogue work once a metadata
        source identified it: files and rows stay, the series gains
        release-source discovery and future monitoring like any catalogue work.

        MangaBaka is the usual identity; a work it does not carry (an English
        anthology, for example) can be identified by another source instead."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE manga
                SET provider='catalogue', source_name=?, source_id=?,
                    source_url=?, future_monitoring_allowed=1,
                    future_monitoring_reason='', updated_at=?
                WHERE id=? AND provider='local'
                """,
                (
                    source_label,
                    str(external_id),
                    source_url or f"https://mangabaka.org/{external_id}",
                    utc_now(),
                    manga_id,
                ),
            )
            return cursor.rowcount > 0

    def set_release_pages(self, chapter_id: str, pages: int) -> bool:
        """Record the real page count of a file already in the library."""

        if pages <= 0:
            return False
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE chapter_release SET pages=?, updated_at=? WHERE id=?",
                (int(pages), utc_now(), chapter_id),
            )
            return cursor.rowcount > 0

    def reader_bookmark(self, manga_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT bookmark.*, chapter.volume, chapter.chapter, chapter.title,
                       chapter.release_unit
                FROM reader_bookmark AS bookmark
                JOIN chapter_release AS chapter ON chapter.id=bookmark.chapter_id
                WHERE bookmark.manga_id=? AND chapter.downloaded=1
                  AND chapter.library_path IS NOT NULL
                """,
                (manga_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_reader_bookmarks(self) -> list[dict[str, Any]]:
        """List saved positions whose books are still in the managed library."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT bookmark.manga_id, bookmark.chapter_id, bookmark.page_index,
                       bookmark.updated_at, chapter.volume, chapter.chapter,
                       chapter.title, chapter.release_unit,
                       COALESCE(NULLIF(trim(manga.title_override), ''),
                                NULLIF(trim(manga.metadata_title), ''),
                                manga.title) AS manga_title,
                       COALESCE(NULLIF(trim(manga.cover_url), ''),
                                CASE WHEN NULLIF(trim(artwork.artwork_sha256), '') IS NOT NULL
                                     THEN '/api/metadata/artwork/' || manga.id || '/series?v='
                                          || artwork.artwork_sha256 END) AS manga_cover_url
                FROM reader_bookmark AS bookmark
                JOIN chapter_release AS chapter ON chapter.id=bookmark.chapter_id
                JOIN manga ON manga.id=bookmark.manga_id
                LEFT JOIN series_metadata AS artwork ON artwork.manga_id=manga.id
                WHERE chapter.manga_id=bookmark.manga_id AND chapter.downloaded=1
                  AND chapter.library_path IS NOT NULL
                ORDER BY bookmark.updated_at DESC, bookmark.manga_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def save_reader_bookmark(
        self, manga_id: str, chapter_id: str, *, page_index: int
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            release = connection.execute(
                """
                SELECT 1 FROM chapter_release
                WHERE id=? AND manga_id=? AND downloaded=1
                  AND library_path IS NOT NULL
                """,
                (chapter_id, manga_id),
            ).fetchone()
            if release is None:
                raise KeyError(chapter_id)
            connection.execute(
                """
                INSERT INTO reader_bookmark(manga_id, chapter_id, page_index, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(manga_id) DO UPDATE SET
                    chapter_id=excluded.chapter_id,
                    page_index=excluded.page_index,
                    updated_at=excluded.updated_at
                """,
                (manga_id, chapter_id, int(page_index), now),
            )
        return self.reader_bookmark(manga_id) or {}

    def clear_reader_bookmark(self, manga_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM reader_bookmark WHERE manga_id=?", (manga_id,)
            )
            return cursor.rowcount > 0

    def list_releases_without_pages(
        self, manga_id: str, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Downloaded files whose page count is still unknown."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chapter_release
                WHERE manga_id=? AND downloaded=1
                  AND library_path IS NOT NULL
                  AND (pages IS NULL OR pages <= 0)
                ORDER BY id
                LIMIT ?
                """,
                (manga_id, int(limit)),
            ).fetchall()
        return [self._decode_chapter(row) for row in rows]

    def list_all_release_sources(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM manga_release_source").fetchall()
        return [dict(row) for row in rows]

    def list_release_sources(
        self, manga_id: str, *, enabled_only: bool = True
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM manga_release_source WHERE manga_id=?"
        parameters: list[object] = [manga_id]
        if enabled_only:
            query += " AND enabled=1"
        query += " ORDER BY lower(provider), lower(source_name), lower(title)"
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode_release_source(row) for row in rows]

    @staticmethod
    def _remote_source_health_key(
        connection: sqlite3.Connection, manga_id: str, mapping: dict[str, Any]
    ) -> str:
        """Use the extension identity used by release ranking, when unambiguous."""

        provider = str(mapping.get("provider") or "").strip().casefold()
        explicit = str(mapping.get("source_key") or "").strip().casefold()
        numeric_key = re.compile(rf"{re.escape(provider)}:-?[0-9]+")
        if numeric_key.fullmatch(explicit):
            return explicit
        name = str(mapping.get("source_name") or "").strip().casefold()
        language = str(mapping.get("language") or "").strip().casefold()
        if name:
            query = (
                "SELECT DISTINCT source_key FROM chapter_release "
                "WHERE lower(provider)=? AND lower(trim(source_name))=? "
                "AND lower(language)=? AND source_key IS NOT NULL"
            )
            for scope in (manga_id, None):
                rows = connection.execute(
                    query + (" AND manga_id=?" if scope is not None else ""),
                    (provider, name, language, scope)
                    if scope is not None
                    else (provider, name, language),
                ).fetchall()
                keys = {
                    str(row["source_key"]).strip().casefold()
                    for row in rows
                    if numeric_key.fullmatch(str(row["source_key"]).strip().casefold())
                }
                if len(keys) == 1:
                    return next(iter(keys))
                if len(keys) > 1:
                    break
        # The first alias is the specific source name when one is known. Never
        # also score its provider: one broken extension is not the whole engine.
        return next(iter(release_source_keys(mapping)), "")

    def record_release_source_result(
        self,
        manga_id: str,
        provider: str,
        provider_manga_id: str,
        *,
        error: str | None = None,
        publication_status: str | None = None,
    ) -> None:
        now = utc_now()
        message = str(error)[:1000] if error else None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM manga_release_source "
                "WHERE manga_id=? AND provider=? AND provider_manga_id=?",
                (manga_id, provider, provider_manga_id),
            ).fetchone()
            if row is None:
                raise KeyError((manga_id, provider, provider_manga_id))
            mapping = dict(row)
            status = (
                str(publication_status).strip().casefold().replace(" ", "_")[:40]
                or None
                if publication_status is not None
                else mapping.get("publication_status")
            )
            connection.execute(
                """
                UPDATE manga_release_source
                SET last_checked_at=?, last_error=?, error_since=?, updated_at=?,
                    publication_status=?
                WHERE manga_id=? AND provider=? AND provider_manga_id=?
                """,
                (
                    now,
                    message,
                    (mapping.get("error_since") or now) if message else None,
                    now,
                    status,
                    manga_id,
                    provider,
                    provider_manga_id,
                ),
            )
            if provider not in NO_REMOTE_PROVIDERS:
                self._record_source_health_connection(
                    connection,
                    self._remote_source_health_key(connection, manga_id, mapping),
                    ok=message is None,
                    reason=message or "",
                )

    def record_primary_source_result(
        self, manga_id: str, *, error: str | None = None
    ) -> None:
        """Record the primary provider request, apart from the whole monitor run."""

        now = utc_now()
        message = str(error)[:1000] if error else None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM manga WHERE id=?", (manga_id,)
            ).fetchone()
            if row is None:
                raise KeyError(manga_id)
            manga = dict(row)
            provider = str(manga.get("provider") or "mangadex")
            if provider in NO_REMOTE_PROVIDERS:
                return
            connection.execute(
                "UPDATE manga SET primary_source_error=?, primary_source_error_since=?, "
                "updated_at=? WHERE id=?",
                (
                    message,
                    (manga.get("primary_source_error_since") or now)
                    if message
                    else None,
                    now,
                    manga_id,
                ),
            )
            identity = {
                **manga,
                "provider": provider,
                "language": manga.get("preferred_language"),
                "source_key": f"{provider}:{manga['source_id']}"
                if manga.get("source_id")
                else None,
            }
            self._record_source_health_connection(
                connection,
                self._remote_source_health_key(connection, manga_id, identity),
                ok=message is None,
                reason=message or "",
            )

    PAUSE_SIGNAL_SOURCES = ("mangabaka", "myanimelist", "mangaupdates")

    def publication_signals(self, manga_id: str) -> dict[str, str]:
        """What each catalogue and the official platform last said about the
        work's publication: ``{"mangabaka": "hiatus", "official": "ongoing"}``.
        AniList is left out on purpose: it reports NANA as releasing."""

        signals: dict[str, str] = {}
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT source, json_extract(data_json, '$.status') AS status, fetched_at
                FROM metadata_source_record
                WHERE manga_id=? AND source IN ('mangabaka', 'myanimelist', 'mangaupdates')
                ORDER BY fetched_at
                """,
                (manga_id,),
            ).fetchall()
        for row in rows:
            status = str(row["status"] or "").strip().casefold()
            if status:
                signals[str(row["source"])] = status
        official = self.official_publication_status(manga_id)
        if official:
            signals["official"] = official
        return signals

    def record_publication_pause(
        self, manga_id: str, *, paused: bool, sources: list[str]
    ) -> dict[str, Any] | None:
        """Remember whether a work is paused; return the previous row when
        the state flipped (the caller announces the change), else None."""

        now = utc_now()
        with self.connect() as connection:
            previous = connection.execute(
                "SELECT paused, sources_json, changed_at FROM publication_pause WHERE manga_id=?",
                (manga_id,),
            ).fetchone()
            if previous is not None and bool(previous["paused"]) == paused:
                return None
            connection.execute(
                """
                INSERT INTO publication_pause (manga_id, paused, sources_json, changed_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(manga_id) DO UPDATE SET
                    paused=excluded.paused, sources_json=excluded.sources_json,
                    changed_at=excluded.changed_at
                """,
                (manga_id, int(paused), json.dumps(sources), now),
            )
        if previous is None:
            return {"paused": None, "sources": [], "changed_at": None}
        return {
            "paused": bool(previous["paused"]),
            "sources": json.loads(previous["sources_json"] or "[]"),
            "changed_at": previous["changed_at"],
        }

    def publication_pause(self, manga_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT paused, sources_json, changed_at FROM publication_pause WHERE manga_id=?",
                (manga_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "paused": bool(row["paused"]),
            "sources": json.loads(row["sources_json"] or "[]"),
            "changed_at": row["changed_at"],
        }

    def official_publication_status(self, manga_id: str) -> str | None:
        """What the work's official platform says about its publication, if
        an enabled official release source reported one on its last refresh."""

        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT publication_status FROM manga_release_source
                WHERE manga_id=? AND enabled=1
                  AND source_role IN ('primary_official', 'secondary_official')
                  AND publication_status IS NOT NULL AND publication_status<>''
                ORDER BY CASE source_role WHEN 'primary_official' THEN 0 ELSE 1 END,
                         updated_at DESC
                LIMIT 1
                """,
                (manga_id,),
            ).fetchone()
        return str(row["publication_status"]) if row else None

    def configure_monitor_mode(
        self, manga_id: str, monitor_mode: str
    ) -> dict[str, Any]:
        """Apply Sonarr-style series monitoring to all releases already known."""

        if monitor_mode not in {"all", "future", "existing", "none"}:
            raise ValueError(f"Unsupported monitor mode: {monitor_mode}")
        monitor_existing = monitor_mode in {"all", "existing"}
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE manga
                SET monitor_mode=?, monitored=?, auto_download=?, updated_at=?
                WHERE id=?
                """,
                (
                    monitor_mode,
                    int(monitor_mode in {"all", "future"}),
                    int(monitor_mode in {"all", "future"}),
                    now,
                    manga_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(manga_id)
            connection.execute(
                "UPDATE chapter_release SET monitored=?, updated_at=? WHERE manga_id=?",
                (int(monitor_existing), now, manga_id),
            )
        return self.get_manga(manga_id)

    def list_monitored_manga(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*,
                       COUNT(DISTINCT CASE WHEN c.id IS NOT NULL THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS chapter_count,
                       COUNT(DISTINCT CASE WHEN c.downloaded = 1 THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS downloaded_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NOT NULL THEN
                               COALESCE(c.volume, '') || ':' || c.chapter
                       END) AS numbered_chapter_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NULL
                            AND NULLIF(trim(c.volume), '') IS NOT NULL THEN c.volume
                       END) AS numbered_volume_count
                FROM manga m
                LEFT JOIN chapter_release c
                  ON c.manga_id = m.id AND c.language = m.preferred_language
                WHERE m.monitor_mode IN ('all', 'future')
                GROUP BY m.id
                ORDER BY lower(COALESCE(
                    NULLIF(trim(m.title_override), ''),
                    NULLIF(trim(m.metadata_title), ''),
                    m.title
                ))
                """
            ).fetchall()
            result = [self._decode_manga(row) for row in rows]
            self._attach_logical_chapter_counts(connection, result)
        return result

    def preferred_download_candidates(
        self, manga_id: str, chapter_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        return self._preferred_missing_releases(
            manga_id, chapter_ids, include_active=False
        )

    def wanted_inputs(self) -> dict[str, Any]:
        """Load every Wanted input in one read snapshot.

        Wanted spans several tables and every series. Reading each table once
        avoids opening hundreds of SQLite connections and lets the service
        normalize each series exactly once.
        """

        release_revisions: dict[str, tuple[int, int, str]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        volume_overrides: dict[str, dict[str, str]] = {}
        chapter_maps: dict[str, list[MapEntry]] = {}
        active_jobs: dict[str, dict[str, Any]] = {}
        blocked: dict[str, dict[str, dict[str, str]]] = {}
        demoted_sources: dict[str, set[str]] = {}
        indexer_volumes: dict[str, set[int]] = {}
        wanted_attempts: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )

        # Backlog modes can synthesize catalogue gaps even when no provider
        # release exists. Other modes matter only when they still own an
        # explicitly monitored, missing release. Applying this predicate at
        # the database boundary avoids decoding the whole library merely to
        # discard it in TankarrService.list_wanted().
        relevant_manga = """
            m.monitor_mode IN ('all', 'existing')
            OR EXISTS (
                SELECT 1 FROM chapter_release wanted_release
                WHERE wanted_release.manga_id = m.id
                  AND wanted_release.language = m.preferred_language
                  AND wanted_release.monitored = 1
                  AND wanted_release.downloaded = 0
            )
            OR EXISTS (
                SELECT 1 FROM volume_monitor_override wanted_volume
                WHERE wanted_volume.manga_id = m.id
                  AND wanted_volume.state = 'monitored'
            )
        """

        with self.connect() as connection:
            manga_rows = connection.execute(
                f"""
                SELECT m.* FROM manga m
                WHERE {relevant_manga}
                ORDER BY lower(COALESCE(
                    NULLIF(trim(m.title_override), ''),
                    NULLIF(trim(m.metadata_title), ''),
                    m.title
                ))
                """
            ).fetchall()
            manga = [self._decode_manga(row) for row in manga_rows]
            # Resolve the monitor predicate once. Repeating its correlated
            # EXISTS clauses for every joined table scanned the release index
            # repeatedly even when just a handful of works were Wanted.
            manga_ids = tuple(str(row["id"]) for row in manga_rows)
            placeholders = ",".join("?" for _ in manga_ids) or "NULL"
            relevant_manga = f"m.id IN ({placeholders})"

            for row in connection.execute(
                f"""
                SELECT c.manga_id, COUNT(*) AS release_count,
                       COALESCE(SUM(c.downloaded), 0) AS downloaded_count,
                       COALESCE(MAX(c.updated_at), '') AS release_updated_at
                FROM chapter_release c
                JOIN manga m ON m.id = c.manga_id
                WHERE c.language = m.preferred_language
                  AND ({relevant_manga})
                GROUP BY c.manga_id
                """,
                manga_ids,
            ):
                release_revisions[str(row["manga_id"])] = (
                    int(row["release_count"] or 0),
                    int(row["downloaded_count"] or 0),
                    str(row["release_updated_at"] or ""),
                )

            for row in connection.execute(
                f"""
                SELECT s.* FROM series_metadata s
                JOIN manga m ON m.id = s.manga_id
                WHERE {relevant_manga}
                """,
                manga_ids,
            ):
                decoded = self._decode_series_metadata_row(row)
                metadata[str(row["manga_id"])] = decoded

            for row in connection.execute(
                f"""
                SELECT o.manga_id, o.volume_key, o.state
                FROM volume_monitor_override o
                JOIN manga m ON m.id = o.manga_id
                WHERE {relevant_manga}
                """,
                manga_ids,
            ):
                volume_overrides.setdefault(str(row["manga_id"]), {})[
                    str(row["volume_key"])
                ] = str(row["state"])

            for row in connection.execute(
                f"""
                SELECT c.manga_id, c.source, c.volumes, c.chapters, c.exact,
                       c.release_date
                FROM series_chapter_map c
                JOIN manga m ON m.id = c.manga_id
                WHERE {relevant_manga}
                ORDER BY c.manga_id, c.exact DESC, c.volumes, c.chapters
                """,
                manga_ids,
            ):
                chapter_maps.setdefault(str(row["manga_id"]), []).append(
                    MapEntry(
                        volumes=tuple(str(row["volumes"]).split(",")),
                        chapters=tuple(str(row["chapters"]).split(",")),
                        exact=bool(row["exact"]),
                        source=str(row["source"]),
                        release_date=row["release_date"],
                    )
                )

            for row in connection.execute(
                f"""
                SELECT j.id AS job_id, j.manga_id, j.chapter_id, j.status,
                       c.volume, c.canonical_chapter,
                       c.release_unit, c.numbering_status
                FROM download_job j
                JOIN manga m ON m.id = j.manga_id
                JOIN chapter_release c ON c.id = j.chapter_id
                WHERE j.status IN ('queued','running','downloading','packaging','importing')
                  AND ({relevant_manga})
                ORDER BY j.id
                """,
                manga_ids,
            ):
                active = dict(row)
                chapter = active.pop("canonical_chapter")
                if (
                    str(active.pop("release_unit") or "chapter") == "volume"
                    or str(active.pop("numbering_status") or "mapped") != "mapped"
                ):
                    chapter = None
                active["chapter"] = chapter
                active_jobs[str(row["chapter_id"])] = active

            for row in connection.execute(
                f"""
                SELECT b.manga_id, b.chapter_id, b.reason, b.created_at
                FROM release_block b
                JOIN manga m ON m.id = b.manga_id
                WHERE {relevant_manga}
                """,
                manga_ids,
            ):
                blocked.setdefault(str(row["manga_id"]), {})[str(row["chapter_id"])] = {
                    "reason": str(row["reason"]),
                    "created_at": str(row["created_at"]),
                }

            for row in connection.execute(
                f"""
                SELECT f.manga_id, f.source_key FROM source_failure f
                JOIN manga m ON m.id = f.manga_id
                WHERE (f.failures >= ? OR f.transient_failures >= ?)
                  AND ({relevant_manga})
                """,
                (
                    self.SOURCE_DEMOTION_FAILURES,
                    self.TRANSIENT_DEMOTION_FAILURES,
                    *manga_ids,
                ),
            ):
                demoted_sources.setdefault(str(row["manga_id"]), set()).add(
                    str(row["source_key"])
                )

            health_rows = [
                dict(row) for row in connection.execute("SELECT * FROM source_health")
            ]

            for row in connection.execute(
                f"""
                SELECT i.manga_id, i.volume FROM indexer_offer i
                JOIN manga m ON m.id = i.manga_id
                WHERE {relevant_manga}
                """,
                manga_ids,
            ):
                try:
                    volume = int(float(row["volume"]))
                except (TypeError, ValueError):
                    continue
                indexer_volumes.setdefault(str(row["manga_id"]), set()).add(volume)

            for row in connection.execute(
                f"""
                SELECT a.manga_id, a.slot_key, a.channel, a.outcome, a.detail,
                       a.attempts, a.first_attempted_at, a.attempted_at
                FROM wanted_attempt a
                JOIN manga m ON m.id = a.manga_id
                WHERE {relevant_manga}
                ORDER BY a.manga_id, a.slot_key, a.attempted_at
                """,
                manga_ids,
            ):
                record = dict(row)
                manga_id = str(record.pop("manga_id"))
                slot_key = str(record.pop("slot_key"))
                wanted_attempts[manga_id][slot_key].append(record)

        return {
            "manga": manga,
            "release_revisions": release_revisions,
            "metadata": metadata,
            "volume_overrides": volume_overrides,
            "chapter_maps": chapter_maps,
            "active_jobs": active_jobs,
            "blocked": blocked,
            "demoted_sources": {
                manga_id: frozenset(sources)
                for manga_id, sources in demoted_sources.items()
            },
            "demoted_global": source_health.unhealthy_keys(health_rows)
            | self.deprioritised_sources,
            "source_health": source_health.components(health_rows),
            "indexer_volumes": indexer_volumes,
            "pending_volumes": self.pending_book_volumes(),
            "wanted_attempts": {
                manga_id: dict(by_slot) for manga_id, by_slot in wanted_attempts.items()
            },
        }

    def preferred_missing_releases(
        self,
        manga_id: str,
        chapter_ids: Iterable[str] | None = None,
        *,
        explain: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        """One preferred monitored release for every logical book absent on disk.

        Active jobs remain visible and carry queue metadata so Wanted can mirror
        Sonarr without making its Search actions enqueue duplicates. Optional
        ``explain`` receives rejection reasons keyed by release id from these
        same gates; it does not change the selected releases.
        """

        return self._preferred_missing_releases(
            manga_id, chapter_ids, include_active=True, explain=explain
        )

    def _preferred_missing_releases(
        self,
        manga_id: str,
        chapter_ids: Iterable[str] | None,
        *,
        include_active: bool,
        explain: dict[str, list[str]] | None = None,
    ) -> list[dict[str, Any]]:
        def rejected(release: dict[str, Any], reason: str) -> None:
            if explain is not None:
                reasons = explain.setdefault(str(release["id"]), [])
                if reason not in reasons:
                    reasons.append(reason)

        def excluded(before, after, reason: str) -> None:
            if explain is not None:
                retained = {str(release["id"]) for release in after}
                for release in before:
                    if str(release["id"]) not in retained:
                        rejected(release, reason)

        manga = self.get_manga(manga_id)
        if manga.get("library_status_override") == "up_to_date":
            if explain is not None:
                for release in self.list_all_chapters(manga_id):
                    rejected(release, "The library is explicitly marked up to date")
            return []
        metadata_row = self.get_series_metadata(manga_id)
        metadata = (metadata_row or {}).get("data") or {}
        all_chapters = self.list_chapters(manga_id, manga["preferred_language"])
        if explain is not None:
            for release in self.list_all_chapters(manga_id):
                if release.get("language") != manga["preferred_language"]:
                    rejected(release, "Different language from the managed edition")
                if release.get("downloaded"):
                    rejected(release, "This release is already owned")
        # A chapter release whose numbering is still unresolved cannot be
        # queued (``create_job`` refuses it), so it is not a candidate: one
        # such release must not abort the queueing of a whole series.
        numbered_chapters = [
            chapter
            for chapter in all_chapters
            if chapter.get("downloaded")
            or str(chapter.get("release_unit") or "chapter") != "chapter"
            or str(chapter.get("numbering_status") or "mapped") == "mapped"
        ]
        excluded(all_chapters, numbered_chapters, "Canonical numbering is unresolved")
        all_chapters = numbered_chapters
        # A book imported by hand names no provider that could fetch it
        # again: once its file is gone the row is a memory, not a candidate
        # (queueing it only ends in "Provider 'manual' is not configured").
        fetchable = [
            chapter
            for chapter in all_chapters
            if chapter.get("downloaded")
            or str(chapter.get("provider") or "") != "manual"
        ]
        excluded(
            all_chapters, fetchable, "Imported by hand: no provider can fetch it again"
        )
        all_chapters = fetchable
        # Under "prefer official" the publisher decides which chapters exist.
        # This is the single gate every download passes through - Wanted, the
        # monitor and discovery all land here - so a scanlator running ahead
        # of the official release is refused once, for all of them.
        if self.source_ranking.acquisition_policy == "prefer_official":
            hosts = official_hosts(
                metadata.get("official_links"),
                language=str(manga.get("preferred_language") or ""),
            )
            frontier = official_frontier(
                all_chapters,
                hosts,
                metadata=metadata,
                status=publication_summary(manga, metadata)["status"],
                chapter_total=(
                    manga.get("expected_count_override")
                    if str(manga.get("expected_count_unit_override") or "").casefold()
                    == "chapter"
                    else None
                ),
            )
            if frontier is not None:
                released_chapters = [
                    chapter
                    for chapter in all_chapters
                    if chapter.get("downloaded")
                    or not beyond_frontier(chapter, frontier)
                ]
                excluded(
                    all_chapters,
                    released_chapters,
                    "Beyond the confirmed official edition frontier",
                )
                all_chapters = released_chapters
        from tankarr.chapter_mapping import (
            _positive_integer,
            canonical_decimal_labels,
            counted_chapter_total,
            effective_edition_book_count,
        )

        publication = publication_summary(manga, metadata)
        continuing = publication["status"] == "continuing" or publication["paused"]
        # A complete edition of a finished work ends the search. The current
        # book count of a continuing work says nothing about its loose tail.
        # Once every book on disk is there - the catalogue's edition or the
        # one the operator declared -
        # no chapter is wanted, whatever unit the series is followed in and
        # whether or not anyone knows which chapters each book holds. Which
        # they hold is a question for the map; it is never a reason to fetch
        # the work a second time in pieces.
        edition_books = effective_edition_book_count(manga) or _positive_integer(
            metadata.get("volume_count")
        )
        if edition_books and not continuing:
            owned_books = {
                number
                for release in all_chapters
                if release.get("downloaded")
                and is_volume_release(release)
                and (number := canonical_number(release.get("volume"))) is not None
            }
            if all(
                str(number) in owned_books for number in range(1, edition_books + 1)
            ):
                books_only = [
                    release for release in all_chapters if is_volume_release(release)
                ]
                excluded(all_chapters, books_only, "The edition on disk is complete")
                all_chapters = books_only

        map_entries = self.chapter_map(manga_id)
        canonical_labels = canonical_decimal_labels(
            map_entries,
            all_chapters,
            chapter_total=counted_chapter_total(manga, metadata),
        )
        _unit_info, chapters = select_releases(
            manga, metadata, all_chapters, canonical_labels=canonical_labels
        )
        if (
            continuing
            and _unit_info["unit"] == "chapters"
            and any(is_volume_release(release) for release in all_chapters)
        ):
            from tankarr.chapter_map import chapters_by_volume, edition_entries
            from tankarr.series_form import _is_webtoon

            # Source books are a fallback too, not just indexer results.
            # One offered chapter must not hide the rest of a volume-only
            # backlog. Estimates are never used to prove coverage here.
            blocked = self.blocked_releases(manga_id)
            chapter_hosts = official_hosts(
                metadata.get("official_links"),
                language=str(manga.get("preferred_language") or ""),
            )
            available_labels = {
                label
                for release in chapters
                if release.get("downloaded")
                or (
                    str(release["id"]) not in blocked
                    and self.source_ranking.best(
                        [release], official_hosts=chapter_hosts
                    )
                    is not None
                )
                if (label := canonical_number(release.get("chapter"))) is not None
            }
            by_volume = chapters_by_volume(edition_entries(map_entries, metadata))
            expected = _unit_info["coverage"]["chapters"]["expected"]
            incomplete = bool(expected and len(available_labels) < expected)
            if not _is_webtoon(metadata):
                chapters.extend(
                    release
                    for release in all_chapters
                    if is_volume_release(release)
                    and (volume := canonical_number(release.get("volume")))
                    and (
                        release.get("downloaded")
                        or (
                            bool(by_volume[volume] - available_labels)
                            if volume in by_volume
                            else incomplete
                        )
                    )
                )
        # An explicit operator map permits chapter acquisition for missing
        # books while the series remains counted and searched as volumes.
        # Run the normal chapter selector so numbering and edition gates still
        # apply; catalogue hints alone never enable this fallback.
        operator_volumes = {
            label: entry.volumes[0]
            for entry in map_entries
            if entry.source == "operator" and entry.exact and len(entry.volumes) == 1
            for label in entry.chapters
        }
        if _unit_info["unit"] == "volumes" and operator_volumes:
            owned_books = {
                volume
                for release in all_chapters
                if release.get("downloaded") and is_volume_release(release)
                for volume in self._volume_coverage(release.get("volume"))
            }
            _chapter_info, fallback = select_releases(
                {**manga, "series_unit_override": "chapters"},
                metadata,
                all_chapters,
                canonical_labels=canonical_labels,
            )
            chapters = [
                *chapters,
                *(
                    {**release, "volume": operator_volumes[label]}
                    for release in fallback
                    if (label := canonical_number(release.get("chapter")))
                    in operator_volumes
                    and operator_volumes[label] not in owned_books
                ),
            ]
        excluded(
            all_chapters,
            chapters,
            "Excluded by the series-unit selector: unit, numbering confirmation or edition coverage",
        )
        volume_overrides = {
            item["volume_key"]: item["state"]
            for item in self.list_volume_monitor_overrides(manga_id)
        }

        def volume_state(chapter: dict[str, Any]) -> str:
            covered = self._volume_coverage(chapter.get("volume"))
            states = {volume_overrides.get(volume, "automatic") for volume in covered}
            if states and states == {"ignored"}:
                return "ignored"
            if "monitored" in states:
                return "monitored"
            return "automatic"

        volume_scoped = volume_scoped_numbering(chapters)
        selected_ids = set(chapter_ids) if chapter_ids is not None else None
        if selected_ids:
            # Discovery reports the releases it just saw. Those name the slots
            # to fill, not the releases to grab: keeping only them would hand
            # the slot to whichever source happened to publish last, ignoring
            # the ranking, the blocks and the sources that keep failing.
            expanded: set[str] = set()
            for candidate_id in selected_ids:
                try:
                    expanded.update(self.slot_release_ids(manga_id, candidate_id))
                except KeyError:
                    continue
                expanded.add(candidate_id)
            selected_ids = expanded or selected_ids
        owned_volumes = {
            str(canonical_number(chapter.get("volume")))
            for chapter in (all_chapters if continuing else chapters)
            if chapter["downloaded"]
            and canonical_number(chapter.get("chapter")) is None
            and canonical_number(chapter.get("volume")) is not None
        }

        def covered_by_owned_volume(chapter: dict[str, Any]) -> bool:
            if not owned_volumes or canonical_number(chapter.get("chapter")) is None:
                return False
            coverage = coverage_for_chapter(
                chapter.get("chapter"), map_entries, owned_volumes
            )
            # A continuing work can have new chapters outside every book.
            # Its estimates and missing mappings cannot close the search.
            return coverage.covered or (
                not continuing and (coverage.unmapped or not map_entries)
            )

        active_by_logical_key: dict[tuple[str, str], dict[str, Any]] = {}
        with self.connect() as connection:
            active_rows = connection.execute(
                """
                SELECT j.id AS job_id, j.status AS job_status,
                       c.id, c.provider, c.volume, c.chapter
                FROM download_job j
                JOIN chapter_release c ON c.id = j.chapter_id
                WHERE j.manga_id=?
                  AND j.status IN ('queued','running','downloading','packaging','importing')
                """,
                (manga_id,),
            ).fetchall()
        for row in active_rows:
            decoded = dict(row)
            active_by_logical_key[
                self._logical_chapter_key(decoded, volume_scoped=volume_scoped)
            ] = decoded

        satisfied_keys = {
            self._logical_chapter_key(chapter, volume_scoped=volume_scoped)
            for chapter in chapters
            if chapter["downloaded"]
        }
        covered_volumes = {
            volume
            for downloaded in chapters
            if downloaded["downloaded"] and not downloaded.get("chapter")
            for volume in self._volume_coverage(downloaded.get("volume"))
        }
        manga_row = manga
        hosts = official_hosts(
            ((metadata_row or {}).get("data") or {}).get("official_links"),
            language=str(manga_row.get("preferred_language") or ""),
        )
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for chapter in chapters:
            if selected_ids is not None and chapter["id"] not in selected_ids:
                rejected(chapter, "Outside the requested acquisition slots")
                continue
            key = self._logical_chapter_key(chapter, volume_scoped=volume_scoped)
            if key in satisfied_keys:
                rejected(chapter, "This logical release is already owned")
                continue
            if not include_active and key in active_by_logical_key:
                rejected(chapter, "An acquisition job already owns this slot")
                continue
            if str(chapter.get("volume") or "") in covered_volumes and (
                not continuing or is_volume_release(chapter)
            ):
                rejected(chapter, "An owned book already covers this volume")
                continue
            if volume_state(chapter) == "ignored":
                rejected(chapter, "This volume is explicitly ignored")
                continue
            if covered_by_owned_volume(chapter):
                rejected(
                    chapter,
                    "Owned-book coverage satisfies this chapter or needs a mapping before another download",
                )
                continue
            grouped.setdefault(key, []).append(chapter)

        blocked = self.blocked_releases(manga_id)
        demoted = self.demoted_sources(manga_id)
        health = self.source_health_components()
        preferred: list[dict[str, Any]] = []

        def _wanted_order(item: tuple[tuple[str, str], list[dict[str, Any]]]):
            (scope, label), _releases = item
            try:
                return (0, Decimal(scope or "0"), Decimal(label))
            except (InvalidOperation, ValueError):
                return (1, Decimal(0), Decimal(0), scope, label)

        for key, releases in sorted(grouped.items(), key=_wanted_order):
            # A decimal the map or the sources make canonical is a chapter:
            # it is followed like one, whatever the specials opt-in says.
            monitored_releases = [
                release
                for release in releases
                if volume_state(release) == "monitored"
                or release["monitored"]
                or (canonical_number(release.get("chapter")) or "") in canonical_labels
            ]
            excluded(releases, monitored_releases, "This release is not monitored")
            if not monitored_releases:
                continue
            unblocked = [
                release
                for release in monitored_releases
                if str(release["id"]) not in blocked
            ]
            excluded(
                monitored_releases,
                unblocked,
                "This release is blocked; review the previous failure before retrying",
            )
            if unblocked:
                chosen = self.source_ranking.best(
                    unblocked,
                    official_hosts=hosts,
                    demoted_sources=demoted,
                    health=health,
                )
                if chosen is None:
                    excluded(
                        unblocked,
                        [],
                        "No source satisfies the current acquisition policy",
                    )
                    continue
                selected = dict(chosen)
                selected["blocked"] = False
                selected["block_reason"] = None
            else:
                # Every candidate for this slot failed before. Keep the slot
                # visible in Wanted without letting automation re-grab a
                # blocked release; History retry unblocks it explicitly.
                chosen = self.source_ranking.best(
                    monitored_releases, official_hosts=hosts, health=health
                )
                if chosen is None:
                    continue
                selected = dict(chosen)
                selected["blocked"] = True
                selected["block_reason"] = blocked.get(str(selected["id"]), {}).get(
                    "reason"
                )
            active = active_by_logical_key.get(key)
            if active:
                for release in monitored_releases:
                    rejected(release, "An acquisition job already owns this slot")
            excluded(
                unblocked or monitored_releases,
                [selected],
                "Another eligible source is preferred for this slot by the current ranking"
                if unblocked
                else "Another blocked release represents this slot in Wanted",
            )
            selected["queue_job_id"] = int(active["job_id"]) if active else None
            selected["queue_status"] = str(active["job_status"]) if active else None
            preferred.append(selected)
        return preferred

    def set_chapter_monitored(self, chapter_id: str, monitored: bool) -> int:
        """Toggle monitoring for every release of one logical chapter."""

        chapter = self.get_chapter(chapter_id)
        chapters = self.list_chapters(chapter["manga_id"], chapter["language"])
        volume_scoped = volume_scoped_numbering(chapters)
        target_key = self._logical_chapter_key(chapter, volume_scoped=volume_scoped)
        release_ids = [
            item["id"]
            for item in chapters
            if self._logical_chapter_key(item, volume_scoped=volume_scoped)
            == target_key
        ]
        if not release_ids:
            raise KeyError(chapter_id)
        with self.connect() as connection:
            placeholders = ",".join("?" for _ in release_ids)
            cursor = connection.execute(
                f"""
                UPDATE chapter_release
                SET monitored=?, updated_at=?
                WHERE id IN ({placeholders})
                """,
                [int(monitored), utc_now(), *release_ids],
            )
        return cursor.rowcount

    def set_chapter_volumes(self, manga_id: str, volumes: dict[str, str | None]) -> int:
        """Give chapter releases the book they belong to (from the book map),
        so their library names sort under the book: ``v002 c015``."""

        if not volumes:
            return 0
        with self.connect() as connection:
            changed = 0
            for release_id, volume in volumes.items():
                changed += connection.execute(
                    "UPDATE chapter_release SET volume=? WHERE id=? AND manga_id=?"
                    " AND volume IS NOT ?",
                    (volume, release_id, manga_id, volume),
                ).rowcount
        return changed

    def wanted_search_states(self) -> dict[str, Any]:
        """Per-series Wanted search cadence (see ``search_cadence``)."""

        from tankarr.search_cadence import SearchState

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT manga_id, last_search_at, next_search_at, attempts, last_found_at"
                " FROM wanted_search_state"
            ).fetchall()
        return {str(row["manga_id"]): SearchState.from_row(dict(row)) for row in rows}

    def save_wanted_search_state(self, state: Any) -> None:
        row = state.as_row()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO wanted_search_state
                    (manga_id, last_search_at, next_search_at, attempts, last_found_at)
                VALUES (:manga_id, :last_search_at, :next_search_at, :attempts, :last_found_at)
                ON CONFLICT(manga_id) DO UPDATE SET
                    last_search_at=excluded.last_search_at,
                    next_search_at=excluded.next_search_at,
                    attempts=excluded.attempts,
                    last_found_at=excluded.last_found_at
                """,
                row,
            )

    def reset_wanted_search_state(self, manga_id: str) -> None:
        """A person touched the series: the next pass looks at it again."""

        with self.connect() as connection:
            connection.execute(
                "DELETE FROM wanted_search_state WHERE manga_id=?", (manga_id,)
            )

    def list_volume_monitor_overrides(self, manga_id: str) -> list[dict[str, str]]:
        """Return explicit per-volume monitoring decisions.

        Absence means ``automatic`` and therefore follows the series/chapter
        profile. Keeping that state implicit makes resetting an override
        lossless and prevents metadata refreshes from manufacturing user
        decisions.
        """

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT volume_key, state, created_at, updated_at
                FROM volume_monitor_override
                WHERE manga_id=?
                ORDER BY CAST(volume_key AS REAL), volume_key
                """,
                (manga_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_volume_monitor_override(
        self, manga_id: str, volume: object, state: str
    ) -> dict[str, str]:
        """Set ``monitored``/``ignored`` or remove the row for ``automatic``."""

        self.get_manga(manga_id)
        volume_key = _canonical_local_number(volume)
        if volume_key is None or len(volume_key) > 64:
            raise ValueError("A valid volume number or label is required")
        normalized_state = str(state or "").strip().casefold()
        if normalized_state not in {"automatic", "monitored", "ignored"}:
            raise ValueError(f"Unsupported volume monitoring state: {state}")
        now = utc_now()
        with self.connect() as connection:
            if normalized_state == "automatic":
                connection.execute(
                    "DELETE FROM volume_monitor_override WHERE manga_id=? AND volume_key=?",
                    (manga_id, volume_key),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO volume_monitor_override (
                        manga_id, volume_key, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(manga_id, volume_key) DO UPDATE SET
                        state=excluded.state,
                        updated_at=excluded.updated_at
                    """,
                    (manga_id, volume_key, normalized_state, now, now),
                )
        return {"volume": volume_key, "state": normalized_state}

    def list_calendar_inputs(self) -> list[dict[str, Any]]:
        """Load every series and its preferred-language release history in bulk.

        Calendar cadence used to open the database two or three times per
        series. One snapshot keeps the calculation consistent and avoids that
        N+1 query pattern while downloads are updating the library.
        """

        with self.read_snapshot():
            return self._list_calendar_inputs_snapshot()

    def _list_calendar_inputs_snapshot(self) -> list[dict[str, Any]]:
        signals: dict[str, dict[str, str]] = defaultdict(dict)
        with self.connect() as connection:
            manga_rows = connection.execute(
                """
                SELECT m.*,
                       sm.manga_id AS calendar_metadata_manga_id,
                       sm.data_json AS calendar_metadata_json,
                       sm.artwork_path AS calendar_artwork_path,
                       sm.artwork_sha256 AS calendar_artwork_sha256
                FROM manga AS m
                LEFT JOIN series_metadata AS sm ON sm.manga_id=m.id
                ORDER BY m.id
                """
            ).fetchall()
            chapter_rows = connection.execute(
                """
                SELECT c.*
                FROM chapter_release AS c
                JOIN manga AS m
                  ON m.id=c.manga_id AND c.language=m.preferred_language
                ORDER BY c.manga_id, c.publish_at, c.id
                """
            ).fetchall()
            for row in connection.execute(
                "SELECT manga_id, source, json_extract(data_json, '$.status') AS status "
                "FROM metadata_source_record "
                "WHERE source IN ('mangabaka','myanimelist','mangaupdates') "
                "ORDER BY fetched_at"
            ):
                status = str(row["status"] or "").strip().casefold()
                if status:
                    signals[str(row["manga_id"])][str(row["source"])] = status
            for row in connection.execute(
                "SELECT manga_id, publication_status FROM manga_release_source "
                "WHERE enabled=1 AND source_role IN ('primary_official','secondary_official') "
                "AND publication_status IS NOT NULL AND publication_status<>'' "
                "ORDER BY CASE source_role WHEN 'primary_official' THEN 0 ELSE 1 END, "
                "updated_at DESC"
            ):
                signals[str(row["manga_id"])].setdefault(
                    "official", str(row["publication_status"])
                )

        records: dict[str, dict[str, Any]] = {}
        for row in manga_rows:
            raw = dict(row)
            metadata_manga_id = raw.pop("calendar_metadata_manga_id", None)
            metadata_json = raw.pop("calendar_metadata_json", None)
            artwork_path = raw.pop("calendar_artwork_path", None)
            artwork_sha256 = raw.pop("calendar_artwork_sha256", None)
            manga = self._decode_manga(raw)
            metadata = (
                {
                    "data": json.loads(metadata_json or "{}"),
                    "artwork_path": artwork_path,
                    "artwork_sha256": artwork_sha256,
                }
                if metadata_manga_id is not None
                else None
            )
            records[str(manga["id"])] = {
                "manga": manga,
                "metadata": metadata,
                "chapters": [],
                "publication_signals": signals.get(str(manga["id"]), {}),
            }

        for row in chapter_rows:
            release = self._decode_chapter(row)
            record = records.get(str(release["manga_id"]))
            if record is None:
                continue
            record["chapters"].append(release)
        return list(records.values())

    def list_calendar_releases(self, start: str, end: str) -> list[dict[str, Any]]:
        """Releases published inside a window, for series in the library."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT c.*,
                       COALESCE(
                           NULLIF(trim(m.title_override), ''),
                           NULLIF(trim(m.metadata_title), ''),
                           m.title
                       ) AS manga_title,
                       COALESCE(
                           NULLIF(trim(m.cover_url), ''),
                           CASE
                               WHEN NULLIF(trim(series_artwork.artwork_sha256), '')
                                    IS NOT NULL
                               THEN '/api/metadata/artwork/' || m.id
                                    || '/series?v=' || series_artwork.artwork_sha256
                           END
                       ) AS manga_cover_url,
                       m.monitor_mode AS manga_monitor_mode
                FROM chapter_release c
                JOIN manga m ON m.id = c.manga_id
                LEFT JOIN series_metadata AS series_artwork
                  ON series_artwork.manga_id = m.id
                WHERE c.language = m.preferred_language
                  AND c.numbering_status = 'mapped'
                  AND (c.release_unit = 'volume' OR c.canonical_chapter IS NOT NULL)
                  AND c.publish_at IS NOT NULL
                  AND c.publish_at >= ? AND c.publish_at <= ?
                ORDER BY c.publish_at DESC
                """,
                (start, end),
            ).fetchall()
        return [self._decode_chapter(row) for row in rows]

    def get_setting_overrides(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute("SELECT key, value FROM setting").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def save_setting(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, updated_at=excluded.updated_at
                """,
                (key, value, utc_now()),
            )

    def apply_setting_changes(
        self,
        changes: dict[str, str],
        *,
        delete_keys: Iterable[str] = (),
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        """Publish a validated settings bundle in one transaction.

        The caller can install staged credentials immediately before commit.
        A failed callback rolls back every database override in the bundle.
        The caller retains responsibility for restoring its file if commit fails.
        """

        now = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "DELETE FROM setting WHERE key=?", ((key,) for key in delete_keys)
            )
            connection.executemany(
                "INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                ((key, value, now) for key, value in changes.items()),
            )
            if before_commit is not None:
                before_commit()

    def delete_setting(self, key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM setting WHERE key=?", (key,))

    def save_metadata_source_record(
        self,
        manga_id: str,
        *,
        entity_type: str,
        entity_key: str,
        source: str,
        external_id: str,
        match_confidence: float,
        match_reason: str,
        data: dict[str, Any],
        raw: dict[str, Any],
    ) -> None:
        """Persist the normalized match and its source snapshot for auditing."""

        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                """
                INSERT INTO metadata_source_record (
                    manga_id, entity_type, entity_key, source, external_id,
                    match_confidence, match_reason, data_json, raw_json, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manga_id, entity_type, entity_key, source) DO UPDATE SET
                    external_id=excluded.external_id,
                    match_confidence=excluded.match_confidence,
                    match_reason=excluded.match_reason,
                    data_json=excluded.data_json,
                    raw_json=excluded.raw_json,
                    fetched_at=excluded.fetched_at
                """,
                (
                    manga_id,
                    entity_type,
                    entity_key,
                    source,
                    external_id,
                    float(match_confidence),
                    match_reason[:1000],
                    json.dumps(data, ensure_ascii=False),
                    json.dumps(raw, ensure_ascii=False),
                    now,
                ),
            )

    def list_manual_metadata_correlations(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            rows = connection.execute(
                """
                SELECT source, external_id, url, created_at, updated_at
                FROM metadata_manual_correlation
                WHERE manga_id=?
                ORDER BY source
                """,
                (manga_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_provider_metadata_correlations(
        self, manga_id: str
    ) -> list[dict[str, Any]]:
        """Return exact identities declared by the series' origin provider."""

        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            rows = connection.execute(
                """
                SELECT source, external_id, url, label, origin_provider,
                       created_at, updated_at
                FROM metadata_provider_correlation
                WHERE manga_id=?
                ORDER BY source
                """,
                (manga_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_manual_metadata_correlations(
        self,
        manga_id: str,
        updates: dict[str, dict[str, str] | None],
    ) -> None:
        """Atomically replace only the explicitly supplied manual overrides."""

        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            for source, correlation in updates.items():
                if correlation is None:
                    connection.execute(
                        "DELETE FROM metadata_manual_correlation "
                        "WHERE manga_id=? AND source=?",
                        (manga_id, source),
                    )
                    continue
                connection.execute(
                    """
                    INSERT INTO metadata_manual_correlation (
                        manga_id, source, external_id, url, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manga_id, source) DO UPDATE SET
                        external_id=excluded.external_id,
                        url=excluded.url,
                        updated_at=excluded.updated_at
                    """,
                    (
                        manga_id,
                        source,
                        correlation["external_id"],
                        correlation["url"],
                        now,
                        now,
                    ),
                )

    def list_metadata_source_records(
        self,
        manga_id: str,
        *,
        entity_type: str | None = None,
        entity_key: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["manga_id=?"]
        values: list[Any] = [manga_id]
        if entity_type is not None:
            clauses.append("entity_type=?")
            values.append(entity_type)
        if entity_key is not None:
            clauses.append("entity_key=?")
            values.append(entity_key)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM metadata_source_record
                WHERE {" AND ".join(clauses)}
                ORDER BY entity_type, entity_key, source
                """,
                values,
            ).fetchall()
        return [self._decode_metadata_source_row(row) for row in rows]

    def catalogue_identity_owners(self) -> dict[str, str]:
        """MangaBaka id → series id, for every work already in the library.

        A work added through Add New keeps its catalogue id in the metadata
        identity, not in ``manga.source_id``, so both are consulted.
        """

        owners: dict[str, str] = {}
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT manga_id, external_id
                FROM metadata_source_record
                WHERE source='mangabaka' AND NULLIF(trim(external_id), '') IS NOT NULL
                """
            ).fetchall()
            for row in rows:
                owners[str(row["external_id"])] = str(row["manga_id"])
            for row in connection.execute(
                "SELECT id, source_id FROM manga "
                "WHERE provider='catalogue' AND NULLIF(trim(source_id), '') IS NOT NULL"
            ).fetchall():
                owners.setdefault(str(row["source_id"]), str(row["id"]))
        return owners

    def create_author(self, author_id: str, display_name: str) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO author (
                    id, display_name, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (author_id, display_name, now, now),
            )
            row = connection.execute(
                "SELECT * FROM author WHERE id=?", (author_id,)
            ).fetchone()
            return self._decode_author_row(connection, row)

    def add_author_alias(
        self,
        author_id: str,
        *,
        normalized_name: str,
        name: str,
        source_url: str,
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            self._require_author(connection, author_id)
            connection.execute(
                """
                INSERT INTO author_alias (
                    author_id, normalized_name, name, source_url, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(author_id, normalized_name) DO UPDATE SET
                    name=excluded.name,
                    source_url=excluded.source_url,
                    updated_at=excluded.updated_at
                """,
                (author_id, normalized_name, name, source_url, now, now),
            )
            connection.execute(
                "UPDATE author SET updated_at=? WHERE id=?", (now, author_id)
            )

    def find_authors_by_alias(self, normalized_name: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT author.*
                FROM author
                JOIN author_alias ON author_alias.author_id = author.id
                WHERE author.merged_into IS NULL
                  AND author_alias.normalized_name=?
                ORDER BY author.created_at, author.id
                """,
                (normalized_name,),
            ).fetchall()
            return [self._decode_author_row(connection, row) for row in rows]

    def get_author(
        self, author_id: str, *, include_works: bool = True
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            requested = author_id
            seen: set[str] = set()
            row = connection.execute(
                "SELECT * FROM author WHERE id=?", (author_id,)
            ).fetchone()
            while row is not None and row["merged_into"]:
                current = str(row["id"])
                if current in seen:
                    raise RuntimeError(f"Author merge cycle detected at {current}")
                seen.add(current)
                row = connection.execute(
                    "SELECT * FROM author WHERE id=?", (row["merged_into"],)
                ).fetchone()
            if row is None:
                return None
            result = self._decode_author_row(
                connection, row, include_works=include_works
            )
            result["redirected_from"] = requested if requested != result["id"] else None
            return result

    def list_authors(
        self, *, include_merged: bool = False, include_works: bool = False
    ) -> list[dict[str, Any]]:
        where = "" if include_merged else "WHERE merged_into IS NULL"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM author {where} ORDER BY lower(display_name), id"
            ).fetchall()
            return [
                self._decode_author_row(connection, row, include_works=include_works)
                for row in rows
            ]

    def list_author_identities(
        self, *, include_merged: bool = False
    ) -> list[dict[str, Any]]:
        """Bulk form used by reconciliation; avoids per-author SQL queries."""

        where = "" if include_merged else "WHERE merged_into IS NULL"
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM author {where} ORDER BY lower(display_name), id"
            ).fetchall()
            result = {
                str(row["id"]): {
                    **dict(row),
                    "aliases": [],
                    "work_ids": [],
                    "work_count": 0,
                    "library_manga_count": 0,
                }
                for row in rows
            }
            if not result:
                return []
            placeholders = ",".join("?" for _ in result)
            for alias in connection.execute(
                f"""
                SELECT author_id, normalized_name, name, source_url
                FROM author_alias
                WHERE author_id IN ({placeholders})
                ORDER BY lower(name), normalized_name
                """,
                tuple(result),
            ).fetchall():
                result[str(alias["author_id"])]["aliases"].append(dict(alias))
            for work in connection.execute(
                f"""
                SELECT author_id, catalogue_id
                FROM author_work
                WHERE author_id IN ({placeholders})
                ORDER BY catalogue_id
                """,
                tuple(result),
            ).fetchall():
                result[str(work["author_id"])]["work_ids"].append(
                    str(work["catalogue_id"])
                )
            for author_id, item in result.items():
                item["work_count"] = len(item["work_ids"])
            for linked in connection.execute(
                f"""
                SELECT author_id, COUNT(*) AS manga_count
                FROM manga_author
                WHERE author_id IN ({placeholders})
                GROUP BY author_id
                """,
                tuple(result),
            ).fetchall():
                result[str(linked["author_id"])]["library_manga_count"] = int(
                    linked["manga_count"]
                )
        return list(result.values())

    def update_author_display_name(self, author_id: str, display_name: str) -> None:
        with self.connect() as connection:
            self._require_author(connection, author_id)
            connection.execute(
                "UPDATE author SET display_name=?, updated_at=? WHERE id=?",
                (display_name, utc_now(), author_id),
            )

    def seed_author_work(self, author_id: str, card: dict[str, Any]) -> None:
        catalogue_id = str(card.get("external_id") or "").strip()
        if not catalogue_id:
            return
        now = utc_now()
        payload = {
            key: value
            for key, value in card.items()
            if key not in {"in_library", "library_manga_id"}
        }
        with self.connect() as connection:
            self._require_author(connection, author_id)
            connection.execute(
                """
                INSERT INTO mangabaka_work (catalogue_id, data_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(catalogue_id) DO UPDATE SET
                    data_json=excluded.data_json,
                    updated_at=excluded.updated_at
                """,
                (catalogue_id, json.dumps(payload, ensure_ascii=False), now),
            )
            connection.execute(
                """
                INSERT INTO author_work (
                    author_id, catalogue_id, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(author_id, catalogue_id) DO UPDATE SET
                    last_seen_at=excluded.last_seen_at
                """,
                (author_id, catalogue_id, now, now),
            )

    def replace_author_works(
        self,
        author_id: str,
        cards: list[dict[str, Any]],
        *,
        refreshed_at: str,
        next_refresh_at: str,
    ) -> None:
        by_id = {
            str(card.get("external_id") or "").strip(): {
                key: value
                for key, value in card.items()
                if key not in {"in_library", "library_manga_id"}
            }
            for card in cards
            if str(card.get("external_id") or "").strip()
        }
        with self.connect() as connection:
            self._require_author(connection, author_id)
            for catalogue_id, payload in by_id.items():
                connection.execute(
                    """
                    INSERT INTO mangabaka_work (catalogue_id, data_json, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(catalogue_id) DO UPDATE SET
                        data_json=excluded.data_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        catalogue_id,
                        json.dumps(payload, ensure_ascii=False),
                        refreshed_at,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO author_work (
                        author_id, catalogue_id, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(author_id, catalogue_id) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at
                    """,
                    (author_id, catalogue_id, refreshed_at, refreshed_at),
                )
            if by_id:
                placeholders = ",".join("?" for _ in by_id)
                connection.execute(
                    f"DELETE FROM author_work WHERE author_id=? "
                    f"AND catalogue_id NOT IN ({placeholders})",
                    (author_id, *by_id),
                )
            else:
                connection.execute(
                    "DELETE FROM author_work WHERE author_id=?", (author_id,)
                )
            connection.execute(
                """
                UPDATE author
                SET last_refreshed_at=?, next_refresh_at=?, refresh_failures=0,
                    last_refresh_error=NULL, updated_at=?
                WHERE id=?
                """,
                (refreshed_at, next_refresh_at, refreshed_at, author_id),
            )

    def record_author_refresh_failure(
        self, author_id: str, *, error: str, next_refresh_at: str
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            self._require_author(connection, author_id)
            connection.execute(
                """
                UPDATE author
                SET refresh_failures=refresh_failures + 1,
                    last_refresh_error=?, next_refresh_at=?, updated_at=?
                WHERE id=?
                """,
                (error[:1000], next_refresh_at, now, author_id),
            )

    def list_authors_due(self, now: str, *, limit: int = 1) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT author.*
                FROM author
                WHERE author.merged_into IS NULL
                  AND EXISTS (
                      SELECT 1 FROM manga_author WHERE manga_author.author_id=author.id
                  )
                  AND (author.next_refresh_at IS NULL OR author.next_refresh_at <= ?)
                ORDER BY
                    CASE WHEN author.next_refresh_at IS NULL THEN 0 ELSE 1 END,
                    author.next_refresh_at,
                    author.created_at
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
            return [self._decode_author_row(connection, row) for row in rows]

    def replace_manga_authors(self, manga_id: str, links: list[dict[str, Any]]) -> None:
        now = utc_now()
        by_author: dict[str, dict[str, set[str]]] = {}
        for link in links:
            author_id = str(link.get("author_id") or "").strip()
            if not author_id:
                continue
            grouped = by_author.setdefault(
                author_id, {"credited_names": set(), "roles": set()}
            )
            grouped["credited_names"].update(
                str(value).strip()
                for value in link.get("credited_names") or []
                if str(value).strip()
            )
            grouped["roles"].update(
                str(value).strip()
                for value in link.get("roles") or []
                if str(value).strip()
            )
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            for author_id, values in by_author.items():
                self._require_author(connection, author_id)
                connection.execute(
                    """
                    INSERT INTO manga_author (
                        manga_id, author_id, credited_names_json, roles_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manga_id, author_id) DO UPDATE SET
                        credited_names_json=excluded.credited_names_json,
                        roles_json=excluded.roles_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        manga_id,
                        author_id,
                        json.dumps(
                            sorted(values["credited_names"]), ensure_ascii=False
                        ),
                        json.dumps(sorted(values["roles"]), ensure_ascii=False),
                        now,
                        now,
                    ),
                )
            if by_author:
                placeholders = ",".join("?" for _ in by_author)
                connection.execute(
                    f"DELETE FROM manga_author WHERE manga_id=? "
                    f"AND author_id NOT IN ({placeholders})",
                    (manga_id, *by_author),
                )
            else:
                connection.execute(
                    "DELETE FROM manga_author WHERE manga_id=?", (manga_id,)
                )

    def list_manga_authors(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT author.id, author.display_name,
                       manga_author.credited_names_json, manga_author.roles_json
                FROM manga_author
                JOIN author ON author.id=manga_author.author_id
                WHERE manga_author.manga_id=? AND author.merged_into IS NULL
                ORDER BY lower(author.display_name), author.id
                """,
                (manga_id,),
            ).fetchall()
        return [
            {
                "id": str(row["id"]),
                "name": str(row["display_name"]),
                "credited_names": json.loads(row["credited_names_json"] or "[]"),
                "roles": json.loads(row["roles_json"] or "[]"),
            }
            for row in rows
        ]

    def merge_authors(
        self,
        target_author_id: str,
        source_author_id: str,
        *,
        reason: str,
        evidence: dict[str, Any],
    ) -> None:
        if target_author_id == source_author_id:
            return
        now = utc_now()
        with self.connect() as connection:
            target = self._require_author(connection, target_author_id)
            source = self._require_author(connection, source_author_id)
            if target["merged_into"] or source["merged_into"]:
                raise ValueError("Author merges require two active author records")
            connection.execute(
                """
                INSERT INTO author_alias (
                    author_id, normalized_name, name, source_url, created_at, updated_at
                )
                SELECT ?, normalized_name, name, source_url, created_at, ?
                FROM author_alias WHERE author_id=?
                ON CONFLICT(author_id, normalized_name) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                (target_author_id, now, source_author_id),
            )
            connection.execute(
                """
                INSERT INTO author_work (
                    author_id, catalogue_id, first_seen_at, last_seen_at
                )
                SELECT ?, catalogue_id, first_seen_at, last_seen_at
                FROM author_work WHERE author_id=?
                ON CONFLICT(author_id, catalogue_id) DO UPDATE SET
                    last_seen_at=MAX(author_work.last_seen_at, excluded.last_seen_at)
                """,
                (target_author_id, source_author_id),
            )
            source_links = connection.execute(
                "SELECT * FROM manga_author WHERE author_id=?",
                (source_author_id,),
            ).fetchall()
            for source_link in source_links:
                manga_id = str(source_link["manga_id"])
                target_link = connection.execute(
                    "SELECT * FROM manga_author WHERE manga_id=? AND author_id=?",
                    (manga_id, target_author_id),
                ).fetchone()
                names = set(json.loads(source_link["credited_names_json"] or "[]"))
                roles = set(json.loads(source_link["roles_json"] or "[]"))
                created_at = str(source_link["created_at"])
                if target_link is not None:
                    names.update(json.loads(target_link["credited_names_json"] or "[]"))
                    roles.update(json.loads(target_link["roles_json"] or "[]"))
                    created_at = min(created_at, str(target_link["created_at"]))
                connection.execute(
                    """
                    INSERT INTO manga_author (
                        manga_id, author_id, credited_names_json, roles_json,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manga_id, author_id) DO UPDATE SET
                        credited_names_json=excluded.credited_names_json,
                        roles_json=excluded.roles_json,
                        updated_at=excluded.updated_at
                    """,
                    (
                        manga_id,
                        target_author_id,
                        json.dumps(sorted(names), ensure_ascii=False),
                        json.dumps(sorted(roles), ensure_ascii=False),
                        created_at,
                        now,
                    ),
                )
            connection.execute(
                "DELETE FROM manga_author WHERE author_id=?", (source_author_id,)
            )
            connection.execute(
                "UPDATE author SET merged_into=? WHERE merged_into=?",
                (target_author_id, source_author_id),
            )
            connection.execute(
                """
                UPDATE author
                SET merged_into=?, next_refresh_at=?, updated_at=?
                WHERE id=?
                """,
                (target_author_id, now, now, source_author_id),
            )
            connection.execute(
                "UPDATE author SET next_refresh_at=?, updated_at=? WHERE id=?",
                (now, now, target_author_id),
            )
            connection.execute(
                """
                INSERT INTO author_merge (
                    source_author_id, target_author_id, reason, evidence_json, merged_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_author_id) DO UPDATE SET
                    target_author_id=excluded.target_author_id,
                    reason=excluded.reason,
                    evidence_json=excluded.evidence_json,
                    merged_at=excluded.merged_at
                """,
                (
                    source_author_id,
                    target_author_id,
                    reason,
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                ),
            )

    def list_metadata_identity_owners(
        self, source: str, external_id: str, *, exclude_manga_id: str
    ) -> list[dict[str, Any]]:
        """Return other series currently claiming one fetched work identity."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT m.*
                FROM metadata_source_record AS record
                JOIN manga AS m ON m.id = record.manga_id
                WHERE record.source=?
                  AND record.external_id=?
                  AND record.entity_type='work'
                  AND record.manga_id<>?
                ORDER BY m.id
                """,
                (source, external_id, exclude_manga_id),
            ).fetchall()
        return [self._decode_manga(row) for row in rows]

    def delete_metadata_source_records(
        self,
        manga_id: str,
        *,
        source: str,
        entity_key: str | None = None,
        identity_only: bool = False,
    ) -> int:
        """Remove stale provider correlations without touching media files.

        ``identity_only`` is used when a series matcher is re-evaluated. It
        deliberately preserves per-volume records owned by the same provider.
        """

        clauses = ["manga_id=?", "source=?"]
        values: list[Any] = [manga_id, source]
        if entity_key is not None:
            clauses.append("entity_key=?")
            values.append(entity_key)
        if identity_only:
            clauses.append("entity_type IN ('series', 'work', 'edition')")
            clauses.append("entity_key='' ")
        with self.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM metadata_source_record WHERE {' AND '.join(clauses)}",
                values,
            )
        return cursor.rowcount

    def save_series_metadata(
        self,
        manga_id: str,
        data: dict[str, Any],
        *,
        artwork_path: str | None,
        artwork_sha256: str | None,
        artwork_media_type: str | None,
        source_status: list[dict[str, Any]],
        error: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                """
                INSERT INTO series_metadata (
                    manga_id, data_json, artwork_path, artwork_sha256,
                    artwork_media_type, source_status_json, last_enriched_at,
                    last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manga_id) DO UPDATE SET
                    data_json=excluded.data_json,
                    artwork_path=excluded.artwork_path,
                    artwork_sha256=excluded.artwork_sha256,
                    artwork_media_type=excluded.artwork_media_type,
                    source_status_json=excluded.source_status_json,
                    last_enriched_at=excluded.last_enriched_at,
                    last_synced_at=NULL,
                    last_error=excluded.last_error
                """,
                (
                    manga_id,
                    json.dumps(data, ensure_ascii=False),
                    artwork_path,
                    artwork_sha256,
                    artwork_media_type,
                    json.dumps(source_status, ensure_ascii=False),
                    now,
                    error[:1000] if error else None,
                ),
            )
            self._refresh_official_edition_sources_connection(connection, manga_id)
            self._reconcile_numbering_connection(connection, manga_id)
        return self.get_series_metadata(manga_id) or {}

    def record_series_metadata_error(
        self, manga_id: str, error: str, source_status: list[dict[str, Any]]
    ) -> None:
        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            current = connection.execute(
                "SELECT data_json FROM series_metadata WHERE manga_id=?", (manga_id,)
            ).fetchone()
            data_json = str(current["data_json"]) if current else "{}"
            connection.execute(
                """
                INSERT INTO series_metadata (
                    manga_id, data_json, source_status_json, last_enriched_at,
                    last_error
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(manga_id) DO UPDATE SET
                    source_status_json=excluded.source_status_json,
                    last_enriched_at=excluded.last_enriched_at,
                    last_error=excluded.last_error
                """,
                (
                    manga_id,
                    data_json,
                    json.dumps(source_status, ensure_ascii=False),
                    now,
                    error[:1000],
                ),
            )

    def mark_series_metadata_synced(self, manga_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE series_metadata SET last_synced_at=?, last_error=NULL "
                "WHERE manga_id=?",
                (utc_now(), manga_id),
            )

    def record_series_sync_error(self, manga_id: str, error: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE series_metadata SET last_error=? WHERE manga_id=?",
                (error[:1000], manga_id),
            )

    def get_series_metadata(self, manga_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM series_metadata WHERE manga_id=?", (manga_id,)
            ).fetchone()
        return self._decode_series_metadata_row(row) if row is not None else None

    def replace_series_artwork_candidates(
        self,
        manga_id: str,
        candidates: list[dict[str, Any]],
        *,
        automatic_candidate_id: str,
    ) -> list[dict[str, Any]]:
        """Persist the current viable covers while retaining a manual choice.

        A temporarily unavailable catalogue must not silently erase the cover
        selected by the user. The selected candidate is therefore retained
        until the user returns to Automatic or chooses another candidate.
        Uploaded covers remain durable alternatives even in Automatic mode.
        """

        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            preferred = connection.execute(
                "SELECT candidate_id FROM series_artwork_preference WHERE manga_id=?",
                (manga_id,),
            ).fetchone()
            preferred_id = str(preferred["candidate_id"]) if preferred else None
            if preferred_id:
                connection.execute(
                    "DELETE FROM series_artwork_candidate "
                    "WHERE manga_id=? AND candidate_id<>? AND source<>'upload'",
                    (manga_id, preferred_id),
                )
            else:
                connection.execute(
                    "DELETE FROM series_artwork_candidate "
                    "WHERE manga_id=? AND source<>'upload'",
                    (manga_id,),
                )
            for candidate in candidates:
                candidate_id = str(candidate["candidate_id"])
                connection.execute(
                    """
                    INSERT INTO series_artwork_candidate (
                        manga_id, candidate_id, source, source_url,
                        artwork_path, artwork_sha256, artwork_media_type,
                        source_width, source_height, score, source_priority,
                        is_automatic, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manga_id, candidate_id) DO UPDATE SET
                        source=excluded.source,
                        source_url=excluded.source_url,
                        artwork_path=excluded.artwork_path,
                        artwork_sha256=excluded.artwork_sha256,
                        artwork_media_type=excluded.artwork_media_type,
                        source_width=excluded.source_width,
                        source_height=excluded.source_height,
                        score=excluded.score,
                        source_priority=excluded.source_priority,
                        is_automatic=excluded.is_automatic,
                        updated_at=excluded.updated_at
                    """,
                    (
                        manga_id,
                        candidate_id,
                        str(candidate["source"]),
                        str(candidate["source_url"]),
                        str(candidate["path"]),
                        str(candidate["sha256"]),
                        str(candidate.get("media_type") or "image/jpeg"),
                        int(candidate["source_width"]),
                        int(candidate["source_height"]),
                        float(candidate["score"]),
                        int(candidate["source_priority"]),
                        int(candidate_id == automatic_candidate_id),
                        now,
                    ),
                )
            connection.execute(
                "UPDATE series_artwork_candidate SET is_automatic=(candidate_id=?) "
                "WHERE manga_id=?",
                (automatic_candidate_id, manga_id),
            )
        return self.list_series_artwork_candidates(manga_id)

    def upsert_series_artwork_candidate(
        self, manga_id: str, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        """Store one durable, non-automatic artwork candidate."""

        candidate_id = str(candidate["candidate_id"])
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                """
                INSERT INTO series_artwork_candidate (
                    manga_id, candidate_id, source, source_url,
                    artwork_path, artwork_sha256, artwork_media_type,
                    source_width, source_height, score, source_priority,
                    is_automatic, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(manga_id, candidate_id) DO UPDATE SET
                    source=excluded.source,
                    source_url=excluded.source_url,
                    artwork_path=excluded.artwork_path,
                    artwork_sha256=excluded.artwork_sha256,
                    artwork_media_type=excluded.artwork_media_type,
                    source_width=excluded.source_width,
                    source_height=excluded.source_height,
                    score=excluded.score,
                    source_priority=excluded.source_priority,
                    is_automatic=0,
                    updated_at=excluded.updated_at
                """,
                (
                    manga_id,
                    candidate_id,
                    str(candidate["source"]),
                    str(candidate["source_url"]),
                    str(candidate["path"]),
                    str(candidate["sha256"]),
                    str(candidate.get("media_type") or "image/jpeg"),
                    int(candidate["source_width"]),
                    int(candidate["source_height"]),
                    float(candidate.get("score") or 0),
                    int(candidate.get("source_priority") or 0),
                    utc_now(),
                ),
            )
            row = connection.execute(
                "SELECT * FROM series_artwork_candidate "
                "WHERE manga_id=? AND candidate_id=?",
                (manga_id, candidate_id),
            ).fetchone()
        if row is None:  # pragma: no cover - guarded by the upsert above
            raise KeyError(candidate_id)
        return self._decode_series_artwork_candidate_row(row)

    def list_series_artwork_candidates(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM series_artwork_candidate
                WHERE manga_id=?
                ORDER BY is_automatic DESC, source_priority DESC, score DESC,
                         source, candidate_id
                """,
                (manga_id,),
            ).fetchall()
        return [self._decode_series_artwork_candidate_row(row) for row in rows]

    def get_series_artwork_preference(self, manga_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT candidate_id FROM series_artwork_preference WHERE manga_id=?",
                (manga_id,),
            ).fetchone()
        return str(row["candidate_id"]) if row else None

    def set_series_artwork_preference(
        self, manga_id: str, candidate_id: str | None
    ) -> None:
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            if candidate_id is None:
                connection.execute(
                    "DELETE FROM series_artwork_preference WHERE manga_id=?",
                    (manga_id,),
                )
                return
            normalized = str(candidate_id).strip()
            found = connection.execute(
                "SELECT 1 FROM series_artwork_candidate "
                "WHERE manga_id=? AND candidate_id=?",
                (manga_id, normalized),
            ).fetchone()
            if found is None:
                raise KeyError(normalized)
            connection.execute(
                """
                INSERT INTO series_artwork_preference (
                    manga_id, candidate_id, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(manga_id) DO UPDATE SET
                    candidate_id=excluded.candidate_id,
                    updated_at=excluded.updated_at
                """,
                (manga_id, normalized, utc_now()),
            )

    def save_volume_metadata(
        self,
        manga_id: str,
        volume_key: str,
        data: dict[str, Any],
        *,
        artwork_path: str | None = None,
        artwork_sha256: str | None = None,
        artwork_media_type: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                """
                INSERT INTO volume_metadata (
                    manga_id, volume_key, data_json, artwork_path,
                    artwork_sha256, artwork_media_type, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(manga_id, volume_key) DO UPDATE SET
                    data_json=excluded.data_json,
                    artwork_path=excluded.artwork_path,
                    artwork_sha256=excluded.artwork_sha256,
                    artwork_media_type=excluded.artwork_media_type,
                    updated_at=excluded.updated_at
                """,
                (
                    manga_id,
                    volume_key,
                    json.dumps(data, ensure_ascii=False),
                    artwork_path,
                    artwork_sha256,
                    artwork_media_type,
                    now,
                ),
            )
        return next(
            item
            for item in self.list_volume_metadata(manga_id)
            if item["volume_key"] == volume_key
        )

    def list_volume_metadata(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM volume_metadata WHERE manga_id=?
                ORDER BY CAST(volume_key AS REAL), volume_key
                """,
                (manga_id,),
            ).fetchall()
        return [self._decode_volume_metadata_row(row) for row in rows]

    def list_manga_needing_metadata(self, stale_before: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*,
                       COUNT(DISTINCT CASE WHEN c.id IS NOT NULL THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS chapter_count,
                       COUNT(DISTINCT CASE WHEN c.downloaded = 1 THEN
                           COALESCE(c.volume, '') || ':' || COALESCE(c.chapter, c.id)
                       END) AS downloaded_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NOT NULL THEN
                               COALESCE(c.volume, '') || ':' || c.chapter
                       END) AS numbered_chapter_count,
                       COUNT(DISTINCT CASE
                           WHEN NULLIF(trim(c.chapter), '') IS NULL
                            AND NULLIF(trim(c.volume), '') IS NOT NULL THEN c.volume
                       END) AS numbered_volume_count
                FROM manga m
                LEFT JOIN chapter_release c
                  ON c.manga_id = m.id AND c.language = m.preferred_language
                LEFT JOIN series_metadata sm ON sm.manga_id = m.id
                WHERE sm.manga_id IS NULL
                   OR sm.last_enriched_at < ?
                   OR sm.last_error IS NOT NULL
                GROUP BY m.id
                ORDER BY CASE WHEN sm.manga_id IS NULL THEN 0 ELSE 1 END,
                         lower(COALESCE(
                             NULLIF(trim(m.title_override), ''),
                             NULLIF(trim(m.metadata_title), ''),
                             m.title
                         ))
                """,
                (stale_before,),
            ).fetchall()
            result = [self._decode_manga(row) for row in rows]
            self._attach_logical_chapter_counts(connection, result)
        return result

    def metadata_overview(self) -> dict[str, int]:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM manga) AS series_total,
                    (SELECT COUNT(*) FROM series_metadata) AS series_enriched,
                    (SELECT COUNT(DISTINCT manga_id)
                     FROM (
                         SELECT manga_id
                         FROM metadata_source_record
                         WHERE entity_key = ''
                           AND entity_type IN ('series', 'work', 'edition')
                           AND source != 'local'
                         UNION
                         SELECT manga_id FROM metadata_provider_correlation
                         UNION
                         SELECT manga_id FROM metadata_manual_correlation
                     )) AS series_with_external_metadata,
                    (SELECT COUNT(*) FROM series_metadata
                     WHERE artwork_path IS NOT NULL) AS series_with_artwork,
                    (SELECT COUNT(*) FROM series_metadata
                     WHERE last_synced_at IS NOT NULL
                       AND last_error IS NULL) AS series_synced,
                    (SELECT COUNT(*) FROM series_metadata
                     WHERE last_error IS NOT NULL) AS series_errors,
                    (SELECT COUNT(*) FROM volume_metadata v
                     WHERE EXISTS (SELECT 1 FROM chapter_release c
                                   WHERE c.manga_id = v.manga_id
                                     AND c.volume = v.volume_key
                                     AND (c.release_unit = 'volume'
                                          OR NULLIF(trim(c.chapter), '') IS NULL)))
                        AS volumes_enriched,
                    (SELECT COUNT(*) FROM volume_metadata v
                     WHERE v.artwork_path IS NOT NULL
                       AND EXISTS (SELECT 1 FROM chapter_release c
                                   WHERE c.manga_id = v.manga_id
                                     AND c.volume = v.volume_key
                                     AND (c.release_unit = 'volume'
                                          OR NULLIF(trim(c.chapter), '') IS NULL)))
                        AS volumes_with_artwork,
                    (SELECT COUNT(*) FROM metadata_source_record) AS source_records,
                    (SELECT COUNT(*) FROM metadata_sync_state
                     WHERE last_error IS NOT NULL) AS sync_errors
                """
            ).fetchone()
        result = {key: int(row[key] or 0) for key in row.keys()}
        result["series_without_external_metadata"] = max(
            0,
            result["series_total"] - result["series_with_external_metadata"],
        )
        return result

    def get_metadata_sync_state(
        self, target_kind: str, target_id: str
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM metadata_sync_state
                WHERE target_kind=? AND target_id=?
                """,
                (target_kind, target_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_metadata_sync_state(
        self,
        target_kind: str,
        target_id: str,
        *,
        payload_sha256: str | None,
        artwork_sha256: str | None,
        error: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO metadata_sync_state (
                    target_kind, target_id, payload_sha256, artwork_sha256,
                    synced_at, last_error
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_kind, target_id) DO UPDATE SET
                    payload_sha256=excluded.payload_sha256,
                    artwork_sha256=excluded.artwork_sha256,
                    synced_at=excluded.synced_at,
                    last_error=excluded.last_error
                """,
                (
                    target_kind,
                    target_id,
                    payload_sha256,
                    artwork_sha256,
                    utc_now(),
                    error[:1000] if error else None,
                ),
            )

    def record_monitor_result(
        self,
        manga_id: str,
        *,
        error: str | None = None,
        initialized: bool = True,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE manga
                SET monitor_initialized=?, last_checked_at=?, last_check_error=?, updated_at=?
                WHERE id=?
                """,
                (
                    int(initialized),
                    utc_now(),
                    error[:1000] if error else None,
                    utc_now(),
                    manga_id,
                ),
            )

    def list_chapters(self, manga_id: str, language: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM chapter_release
                WHERE manga_id=? AND language=?
                ORDER BY
                    CASE WHEN CAST(volume AS REAL) IS NULL THEN 1 ELSE 0 END,
                    CAST(volume AS REAL),
                    CASE WHEN CAST(chapter AS REAL) IS NULL THEN 1 ELSE 0 END,
                    CAST(chapter AS REAL),
                    publish_at,
                    id
                """,
                (manga_id, language),
            ).fetchall()
        return [self._decode_chapter(row) for row in rows]

    def get_chapter(self, chapter_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM chapter_release WHERE id=?", (chapter_id,)
            ).fetchone()
        if row is None:
            raise KeyError(chapter_id)
        return self._decode_chapter(row)

    def list_chapter_summaries(
        self, manga_id: str, language: str
    ) -> list[dict[str, Any]]:
        """The fields needed to select canonical Wanted candidates."""

        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {CHAPTER_SUMMARY_COLUMNS} FROM chapter_release c
                WHERE c.manga_id=? AND c.language=?
                ORDER BY
                    CASE WHEN CAST(c.volume AS REAL) IS NULL THEN 1 ELSE 0 END,
                    CAST(c.volume AS REAL),
                    CASE WHEN CAST(c.chapter AS REAL) IS NULL THEN 1 ELSE 0 END,
                    CAST(c.chapter AS REAL), c.publish_at, c.id
                """,
                (manga_id, language),
            ).fetchall()
        return [decode_chapter_summary(row) for row in rows]

    def chapters_by_ids(self, chapter_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = tuple(dict.fromkeys(chapter_ids))
        if not ids:
            return {}
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM chapter_release WHERE id IN ({','.join('?' for _ in ids)})",
                ids,
            ).fetchall()
        return {str(row["id"]): self._decode_chapter(row) for row in rows}

    def list_all_chapters(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            rows = connection.execute(
                """
                SELECT * FROM chapter_release
                WHERE manga_id=?
                ORDER BY language, volume, chapter, publish_at, id
                """,
                (manga_id,),
            ).fetchall()
        return [self._decode_chapter(row) for row in rows]

    def list_downloaded_chapters(
        self, manga_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """Return only releases that currently own a library file.

        Startup organization must inspect every owned file, but loading the
        complete source inventory is wasteful: a mature installation can have
        hundreds of thousands of remote candidates for a much smaller local
        library.
        """

        selected = tuple(dict.fromkeys(str(item) for item in (manga_ids or ())))
        scope = ""
        parameters: tuple[str, ...] = ()
        if manga_ids is not None:
            if not selected:
                return []
            scope = f" AND manga_id IN ({','.join('?' for _ in selected)})"
            parameters = selected
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chapter_release WHERE downloaded=1" + scope,
                parameters,
            ).fetchall()
        return [self._decode_chapter(row) for row in rows]

    def count_chapters(self, manga_ids: Iterable[str] | None = None) -> int:
        selected = tuple(dict.fromkeys(str(item) for item in (manga_ids or ())))
        scope = ""
        parameters: tuple[str, ...] = ()
        if manga_ids is not None:
            if not selected:
                return 0
            scope = f" WHERE manga_id IN ({','.join('?' for _ in selected)})"
            parameters = selected
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM chapter_release" + scope,
                parameters,
            ).fetchone()
        return int(row["count"] if row is not None else 0)

    def library_file_references(self) -> list[tuple[dict, dict, list[str]]]:
        """File claims only, without source inventories or logical UI counts."""
        with self.connect() as connection:
            jobs: dict[str, list[str]] = defaultdict(list)
            for row in connection.execute(
                "SELECT chapter_id, result_path FROM download_job "
                "WHERE result_path IS NOT NULL"
            ):
                jobs[str(row["chapter_id"])].append(str(row["result_path"]))
            rows = connection.execute(
                "SELECT * FROM chapter_release WHERE downloaded=1 "
                "OR (library_path IS NOT NULL AND library_path!='') OR id IN "
                "(SELECT chapter_id FROM download_job WHERE result_path IS NOT NULL)"
            ).fetchall()
            mangas = {
                str(row["id"]): self._decode_manga(row)
                for row in connection.execute("SELECT * FROM manga")
            }
        return [
            (
                mangas[str(row["manga_id"])],
                self._decode_chapter(row),
                jobs.get(str(row["id"]), []),
            )
            for row in rows
        ]

    def list_volume_chapters(
        self, manga_id: str, volume: str, language: str
    ) -> list[dict[str, Any]]:
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            rows = connection.execute(
                """
                SELECT * FROM chapter_release
                WHERE manga_id=? AND volume=? AND language=?
                ORDER BY language, chapter, publish_at, id
                """,
                (manga_id, volume, language),
            ).fetchall()
        if not rows:
            raise KeyError((manga_id, volume))
        return [self._decode_chapter(row) for row in rows]

    def list_job_result_paths(self, chapter_ids: Iterable[str]) -> dict[str, list[str]]:
        ids = list(dict.fromkeys(str(chapter_id) for chapter_id in chapter_ids))
        if not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chapter_id, result_path
                FROM download_job
                WHERE chapter_id IN ({placeholders}) AND result_path IS NOT NULL
                """,
                ids,
            ).fetchall()
        paths: dict[str, list[str]] = {}
        for row in rows:
            paths.setdefault(str(row["chapter_id"]), []).append(str(row["result_path"]))
        return paths

    def assert_manga_deletion_ready(self, manga_id: str) -> None:
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            self._raise_for_active_jobs(connection, "job.manga_id=?", (manga_id,))
            self._raise_for_active_torrent_downloads(connection, manga_id)

    def assert_chapter_file_deletion_ready(
        self, manga_id: str, chapter_id: str
    ) -> None:
        with self.connect() as connection:
            self._require_chapter(connection, manga_id, chapter_id)
            self._raise_for_active_jobs(connection, "job.chapter_id=?", (chapter_id,))

    def assert_volume_file_deletion_ready(
        self, manga_id: str, volume: str, language: str
    ) -> None:
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            rows = connection.execute(
                """
                SELECT id FROM chapter_release
                WHERE manga_id=? AND volume=? AND language=?
                """,
                (manga_id, volume, language),
            ).fetchall()
            if not rows:
                raise KeyError((manga_id, volume, language))
            chapter_ids = [str(row["id"]) for row in rows]
            placeholders = ",".join("?" for _ in chapter_ids)
            self._raise_for_active_jobs(
                connection,
                f"job.chapter_id IN ({placeholders})",
                tuple(chapter_ids),
            )

    def create_deletion_operation(
        self,
        operation_id: str,
        manifest_path: Path,
        library_paths: Iterable[str] = (),
        *,
        disposition: str = "delete",
    ) -> None:
        if disposition not in {"delete", "retain"}:
            raise ValueError("Invalid deletion disposition")
        now = utc_now()
        encoded_paths = json.dumps(
            list(dict.fromkeys(str(path) for path in library_paths)),
            ensure_ascii=False,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO deletion_operation (
                    id, state, disposition, manifest_path, library_paths_json,
                    created_at, updated_at
                ) VALUES (?, 'prepared', ?, ?, ?, ?, ?)
                """,
                (
                    operation_id,
                    disposition,
                    str(manifest_path),
                    encoded_paths,
                    now,
                    now,
                ),
            )

    def get_deletion_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM deletion_operation WHERE id=?", (operation_id,)
            ).fetchone()
        return self._deletion_operation_from_row(row) if row is not None else None

    def mark_deletion_replacement(
        self, operation_id: str, chapter_id: str, archive_sha256: str
    ) -> None:
        """Bind a prepared retirement receipt to its already published replacement.

        Call in the publication transaction before committing the receipt, so
        reader recovery can distinguish a replaced path from an unsafe restore.
        """
        if not self._valid_sha256(archive_sha256):
            raise ValueError("Invalid replacement archive hash")
        with self.connect() as connection:
            chapter = connection.execute(
                "SELECT downloaded, library_sha256 FROM chapter_release WHERE id=?",
                (chapter_id,),
            ).fetchone()
            if (
                chapter is None
                or not chapter["downloaded"]
                or chapter["library_sha256"] != archive_sha256
            ):
                raise ValueError(
                    "Replacement must be published before retiring its predecessor"
                )
            changed = connection.execute(
                "UPDATE deletion_operation SET replacement_chapter_id=?, replacement_sha256=? "
                "WHERE id=? AND state='prepared' AND disposition='retain'",
                (chapter_id, archive_sha256, operation_id),
            ).rowcount
            if changed != 1:
                raise ValueError("Replacement receipt must be prepared for retention")

    def owned_library_paths(self, paths: Iterable[str]) -> set[str]:
        """Which of these recorded library paths a downloaded release owns now.

        A deletion receipt names a path that was emptied. If a release has
        since been filed at that very name, the path is not a deleted book
        that came back: it is a different book the library owns, and the old
        receipt no longer describes anything.
        """

        wanted = [str(path) for path in paths if str(path)]
        if not wanted:
            return set()
        owned: set[str] = set()
        with self.connect() as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                rows = connection.execute(
                    "SELECT library_path FROM chapter_release "
                    "WHERE downloaded=1 AND library_path IS NOT NULL "
                    f"AND library_path IN ({','.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
                owned.update(str(row["library_path"]) for row in rows)
        return owned

    def list_deletion_operations(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM deletion_operation ORDER BY created_at, id"
            ).fetchall()
        return [self._deletion_operation_from_row(row) for row in rows]

    def record_deletion_reconciliation_failure(
        self, operation_ids: Iterable[str], error: str
    ) -> None:
        identifiers = list(dict.fromkeys(str(item) for item in operation_ids))
        if not identifiers:
            return
        placeholders = ",".join("?" for _ in identifiers)
        with self.connect() as connection:
            connection.execute(
                f"""
                UPDATE deletion_operation
                SET komga_attempts=komga_attempts+1,
                    komga_last_error=?, updated_at=?
                WHERE id IN ({placeholders}) AND state='committed'
                """,
                (error[:2000], utc_now(), *identifiers),
            )

    def delete_deletion_operation(self, operation_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "DELETE FROM deletion_operation WHERE id=?", (operation_id,)
            )

    @staticmethod
    def _deletion_operation_from_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            paths = json.loads(result.pop("library_paths_json", "[]"))
        except json.JSONDecodeError:
            paths = None
        result["library_paths"] = paths
        return result

    def delete_manga_record(
        self,
        manga_id: str,
        deletion_operation_id: str | None = None,
        *,
        deferred_scope: tuple[str, str] | None = None,
    ) -> dict[str, int]:
        """Delete a manga and all remaining chapter/job state."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_manga(connection, manga_id)
            self._raise_for_active_jobs(connection, "job.manga_id=?", (manga_id,))
            self._raise_for_active_torrent_downloads(connection, manga_id)
            if deferred_scope is not None:
                request_id, library_root = deferred_scope
                manga = self._decode_manga(
                    connection.execute(
                        "SELECT * FROM manga WHERE id=?", (manga_id,)
                    ).fetchone()
                )
                chapters = [
                    self._decode_chapter(row)
                    for row in connection.execute(
                        "SELECT * FROM chapter_release WHERE manga_id=?", (manga_id,)
                    )
                ]
                job_paths: dict[str, list[str]] = {}
                for row in connection.execute(
                    "SELECT chapter_id, result_path FROM download_job "
                    "WHERE manga_id=? AND result_path IS NOT NULL",
                    (manga_id,),
                ):
                    job_paths.setdefault(row["chapter_id"], []).append(
                        row["result_path"]
                    )
                scope = {
                    "manga": manga,
                    "chapters": chapters,
                    "job_paths": job_paths,
                    "library_root": library_root,
                    "requested_at_ns": time_ns(),
                }
                connection.execute(
                    "INSERT INTO series_deletion_request "
                    "(id,manga_id,scope_json,created_at) VALUES (?,?,?,?)",
                    (request_id, manga_id, json.dumps(scope), utc_now()),
                )
            chapter_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM chapter_release WHERE manga_id=?",
                    (manga_id,),
                ).fetchone()[0]
            )
            jobs = connection.execute(
                "DELETE FROM download_job WHERE manga_id=?", (manga_id,)
            ).rowcount
            connection.execute("DELETE FROM manga WHERE id=?", (manga_id,))
            self._commit_deletion_operation(connection, deletion_operation_id)
        return {"chapters_deleted": chapter_count, "jobs_deleted": int(jobs)}

    def pending_series_deletions(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM series_deletion_request ORDER BY created_at"
            ).fetchall()
        return [{**dict(row), "scope": json.loads(row["scope_json"])} for row in rows]

    def finish_series_deletion(self, request_id: str, operation_id: str | None) -> None:
        with self.connect() as connection:
            self._commit_deletion_operation(connection, operation_id)
            connection.execute(
                "DELETE FROM series_deletion_request WHERE id=?", (request_id,)
            )

    def retry_series_deletion(
        self, request_id: str, error: str, retry_after: float
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE series_deletion_request SET attempts=attempts+1, "
                "last_error=?,retry_after=? WHERE id=?",
                (error[:2000], retry_after, request_id),
            )

    def reset_chapter_file(
        self,
        manga_id: str,
        chapter_id: str,
        deletion_operation_id: str | None = None,
    ) -> dict[str, int]:
        """Reset downloaded state and remove all history for one chapter.

        The import content ledger goes with it. An import refuses content whose
        hash differs from the one recorded for that slot, so a deletion that
        kept ``local_import_sha256`` left the slot unusable for good: the file
        was gone and nothing could take its place.
        """

        with self.connect() as connection:
            self._require_chapter(connection, manga_id, chapter_id)
            self._raise_for_active_jobs(connection, "job.chapter_id=?", (chapter_id,))
            self._forget_wanted_after_file_reset(connection, manga_id, [chapter_id])
            jobs = connection.execute(
                "DELETE FROM download_job WHERE chapter_id=?", (chapter_id,)
            ).rowcount
            connection.execute(
                """
                UPDATE chapter_release
                SET downloaded=0, library_path=NULL, library_sha256=NULL,
                    local_import_sha256=NULL, updated_at=?
                WHERE id=? AND manga_id=?
                """,
                (utc_now(), chapter_id, manga_id),
            )
            self._commit_deletion_operation(connection, deletion_operation_id)
        return {"chapters_reset": 1, "jobs_deleted": int(jobs)}

    def reset_chapter_files(
        self,
        manga_id: str,
        chapter_ids: list[str],
        deletion_operation_id: str | None = None,
    ) -> dict[str, int]:
        """Reset downloaded state, history and import content ledger for a set of releases."""

        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            ids = list(dict.fromkeys(str(value) for value in chapter_ids))
            if not ids:
                raise KeyError((manga_id, "chapters"))
            placeholders = ",".join("?" for _ in ids)
            found = {
                str(row["id"])
                for row in connection.execute(
                    f"SELECT id FROM chapter_release WHERE manga_id=? "
                    f"AND id IN ({placeholders})",
                    (manga_id, *ids),
                ).fetchall()
            }
            missing = [value for value in ids if value not in found]
            if missing:
                raise KeyError((manga_id, missing[0]))
            self._raise_for_active_jobs(
                connection, f"job.chapter_id IN ({placeholders})", tuple(ids)
            )
            self._forget_wanted_after_file_reset(connection, manga_id, ids)
            jobs = connection.execute(
                f"DELETE FROM download_job WHERE chapter_id IN ({placeholders})", ids
            ).rowcount
            connection.execute(
                f"""
                UPDATE chapter_release
                SET downloaded=0, library_path=NULL, library_sha256=NULL,
                    local_import_sha256=NULL, updated_at=?
                WHERE manga_id=? AND id IN ({placeholders})
                """,
                (utc_now(), manga_id, *ids),
            )
            self._commit_deletion_operation(connection, deletion_operation_id)
        return {"chapters_reset": len(ids), "jobs_deleted": int(jobs)}

    def reset_volume_files(
        self,
        manga_id: str,
        volume: str,
        language: str,
        deletion_operation_id: str | None = None,
    ) -> dict[str, int]:
        """Reset downloaded state, history and import content ledger for a whole volume."""

        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            chapter_rows = connection.execute(
                """
                SELECT id FROM chapter_release
                WHERE manga_id=? AND volume=? AND language=?
                """,
                (manga_id, volume, language),
            ).fetchall()
            if not chapter_rows:
                raise KeyError((manga_id, volume))
            chapter_ids = [str(row["id"]) for row in chapter_rows]
            placeholders = ",".join("?" for _ in chapter_ids)
            self._raise_for_active_jobs(
                connection,
                f"job.chapter_id IN ({placeholders})",
                tuple(chapter_ids),
            )
            self._forget_wanted_after_file_reset(connection, manga_id, chapter_ids)
            jobs = connection.execute(
                f"DELETE FROM download_job WHERE chapter_id IN ({placeholders})",
                chapter_ids,
            ).rowcount
            connection.execute(
                """
                UPDATE chapter_release
                SET downloaded=0, library_path=NULL, library_sha256=NULL,
                    local_import_sha256=NULL, updated_at=?
                WHERE manga_id=? AND volume=? AND language=?
                """,
                (utc_now(), manga_id, volume, language),
            )
            self._commit_deletion_operation(connection, deletion_operation_id)
        return {
            "chapters_reset": len(chapter_ids),
            "jobs_deleted": int(jobs),
        }

    @staticmethod
    def _forget_wanted_after_file_reset(
        connection: sqlite3.Connection, manga_id: str, chapter_ids: list[str]
    ) -> None:
        """A deleted import is no longer on its way; reopen its wanted slots."""

        from tankarr.wanted_recovery import slot_key_for

        placeholders = ",".join("?" for _ in chapter_ids)
        rows = connection.execute(
            f"SELECT id, chapter, volume FROM chapter_release WHERE manga_id=? "
            f"AND id IN ({placeholders})",
            (manga_id, *chapter_ids),
        ).fetchall()
        slot_keys = {slot_key_for(dict(row)) for row in rows}
        if slot_keys:
            keys = sorted(slot_keys)
            key_placeholders = ",".join("?" for _ in keys)
            connection.execute(
                f"DELETE FROM wanted_attempt WHERE manga_id=? "
                f"AND slot_key IN ({key_placeholders})",
                (manga_id, *keys),
            )
        connection.execute(
            "DELETE FROM wanted_search_state WHERE manga_id=?", (manga_id,)
        )

    def mark_chapter_downloaded(
        self, chapter_id: str, library_path: Path, library_sha256: str | None = None
    ) -> None:
        if library_sha256 is not None and not self._valid_sha256(library_sha256):
            raise ValueError("Invalid chapter library SHA-256")
        with self.connect() as connection:
            chapter = connection.execute(
                """
                SELECT id, manga_id, language, volume, chapter
                FROM chapter_release
                WHERE id=?
                """,
                (chapter_id,),
            ).fetchone()
            if chapter is None:
                raise KeyError(chapter_id)
            cursor = connection.execute(
                """
                UPDATE chapter_release
                SET downloaded=1, library_path=?, library_sha256=?, updated_at=?
                WHERE id=?
                """,
                (str(library_path), library_sha256, utc_now(), chapter_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(chapter_id)
            sibling_ids = self._download_slot_release_ids(connection, chapter)
            placeholders = ",".join("?" for _ in sibling_ids)
            connection.execute(
                f"""
                DELETE FROM download_job
                WHERE status='queued'
                  AND chapter_id IN ({placeholders})
                """,
                sibling_ids,
            )

    def forget_duplicate_download(self, chapter_id: str) -> None:
        """Release the library ledger of a chapter another copy already covers.

        The library organizer calls this when two downloads of the same chapter
        want one library name: the release stays known, only its file ledger
        (downloaded flag, path, digest) is cleared so the kept copy owns the
        name.

        Its finished jobs are cleared with it. They recorded the same path,
        and a job pointing at a file another chapter now owns makes every
        later audit of that series refuse to touch it.
        """

        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE chapter_release
                SET downloaded=0, library_path=NULL, library_sha256=NULL, updated_at=?
                WHERE id=? AND downloaded=1
                """,
                (now, chapter_id),
            )
            connection.execute(
                """
                UPDATE download_job
                SET result_path=NULL, planned_path=NULL, updated_at=?
                WHERE chapter_id=?
                """,
                (now, chapter_id),
            )

    def publish_local_chapter(
        self,
        manga_id: str,
        chapter: dict[str, Any],
        library_path: Path,
        library_sha256: str,
        local_import_sha256: str,
        *,
        allow_catalogue_series: bool = False,
    ) -> dict[str, Any]:
        """Publish local chapter metadata and its file ledger in one transaction."""

        if not self._valid_sha256(library_sha256):
            raise ValueError("Invalid local chapter library SHA-256")
        if not self._valid_sha256(local_import_sha256):
            raise ValueError("Invalid local import content SHA-256")
        if chapter.get("provider") != "local":
            raise ValueError("Local imports must use the local provider")
        now = utc_now()
        with self.connect() as connection:
            manga = connection.execute(
                "SELECT provider FROM manga WHERE id=?", (manga_id,)
            ).fetchone()
            if manga is None:
                raise KeyError(manga_id)
            if manga["provider"] != "local" and not (
                allow_catalogue_series and manga["provider"] == "catalogue"
            ):
                raise ValueError("Refusing to attach a local import to a remote series")
            existing = connection.execute(
                "SELECT manga_id, provider FROM chapter_release WHERE id=?",
                (chapter["id"],),
            ).fetchone()
            if existing is not None and (
                existing["manga_id"] != manga_id or existing["provider"] != "local"
            ):
                raise ValueError(
                    "Local chapter identity collides with another chapter release"
                )
            connection.execute(
                """
                INSERT INTO chapter_release (
                    id, manga_id, volume, chapter, source_chapter,
                    canonical_chapter, numbering_status, numbering_method,
                    numbering_confidence, numbering_evidence_json,
                    numbering_version, title, language, provider,
                    release_unit, groups_json, publish_at, source_url, pages, version,
                    downloaded, library_path, library_sha256,
                    local_import_sha256, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'mapped', 'local_import', 1.0, '{}', 1,
                          ?, ?, 'local', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    volume=excluded.volume,
                    chapter=excluded.chapter,
                    source_chapter=excluded.source_chapter,
                    canonical_chapter=excluded.canonical_chapter,
                    numbering_status='mapped',
                    numbering_method='local_import',
                    numbering_confidence=1.0,
                    numbering_evidence_json='{}',
                    numbering_version=1,
                    title=excluded.title,
                    language=excluded.language,
                    release_unit=excluded.release_unit,
                    groups_json=excluded.groups_json,
                    publish_at=excluded.publish_at,
                    source_url=excluded.source_url,
                    pages=excluded.pages,
                    version=excluded.version,
                    downloaded=1,
                    library_path=excluded.library_path,
                    library_sha256=excluded.library_sha256,
                    local_import_sha256=excluded.local_import_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    chapter["id"],
                    manga_id,
                    chapter.get("volume"),
                    chapter.get("chapter"),
                    chapter.get("chapter"),
                    chapter.get("chapter"),
                    chapter.get("title", ""),
                    chapter["language"],
                    chapter.get("release_unit")
                    or (
                        "volume"
                        if chapter.get("volume") and not chapter.get("chapter")
                        else "chapter"
                    ),
                    json.dumps(chapter.get("groups", []), ensure_ascii=False),
                    chapter.get("publish_at"),
                    chapter.get("source_url", ""),
                    chapter.get("pages"),
                    chapter.get("version"),
                    str(library_path),
                    library_sha256,
                    local_import_sha256,
                    now,
                    now,
                ),
            )
        return self.get_chapter(str(chapter["id"]))

    def publish_external_chapter(
        self,
        manga_id: str,
        chapter: dict[str, Any],
        library_path: Path,
        library_sha256: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        """Attach a validated external archive to an existing canonical series."""

        provider = str(chapter.get("provider") or "")
        if provider not in EXTERNAL_IMPORT_PROVIDERS:
            raise ValueError("Unsupported external import provider")
        if not self._valid_sha256(library_sha256) or not self._valid_sha256(
            content_sha256
        ):
            raise ValueError("Invalid external import SHA-256 ledger")
        from tankarr.assembly_provenance import assembly_provenance, proven_assembly

        provenance = (
            assembly_provenance(chapter.get("assembled_from"))
            if provider == "assembled"
            else None
        )
        if provider == "assembled" and provenance is None:
            raise ValueError("An assembled book requires valid chapter provenance")
        now = utc_now()
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            existing = connection.execute(
                "SELECT * FROM chapter_release WHERE id=?",
                (chapter["id"],),
            ).fetchone()
            if existing is not None and (
                str(existing["manga_id"]) != manga_id
                or (
                    str(existing["provider"]) != provider
                    and not (
                        existing["provider"] == "translated"
                        and provider != "translated"
                    )
                    and not (
                        provider != "assembled"
                        and (
                            proven_assembly(dict(existing))
                            or (
                                existing["provider"] == "assembled"
                                and not existing["downloaded"]
                                and assembly_provenance(existing["assembled_from"])
                            )
                        )
                    )
                    and not (
                        not existing["downloaded"]
                        and not existing["library_path"]
                        and not existing["library_sha256"]
                        and not existing["local_import_sha256"]
                    )
                )
            ):
                raise ValueError(
                    "External chapter identity collides with another release"
                )
            connection.execute(
                """
                INSERT INTO chapter_release (
                    id, manga_id, volume, chapter, source_chapter,
                    canonical_chapter, numbering_status, numbering_method,
                    numbering_confidence, numbering_evidence_json,
                    numbering_version, title, language, provider,
                    release_unit, groups_json, publish_at, source_url, pages, version,
                    downloaded, library_path, library_sha256,
                    local_import_sha256, first_seen_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'mapped', 'external_import', 1.0, '{}', 1,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    volume=excluded.volume,
                    chapter=excluded.chapter,
                    source_chapter=excluded.source_chapter,
                    canonical_chapter=excluded.canonical_chapter,
                    numbering_status='mapped',
                    numbering_method='external_import',
                    numbering_confidence=1.0,
                    numbering_evidence_json='{}',
                    numbering_version=1,
                    title=excluded.title,
                    provider=excluded.provider,
                    language=excluded.language,
                    release_unit=excluded.release_unit,
                    groups_json=excluded.groups_json,
                    publish_at=excluded.publish_at,
                    source_url=excluded.source_url,
                    pages=excluded.pages,
                    version=excluded.version,
                    downloaded=1,
                    library_path=excluded.library_path,
                    library_sha256=excluded.library_sha256,
                    local_import_sha256=excluded.local_import_sha256,
                    updated_at=excluded.updated_at
                """,
                (
                    chapter["id"],
                    manga_id,
                    chapter.get("volume"),
                    chapter.get("chapter"),
                    chapter.get("chapter"),
                    chapter.get("chapter"),
                    chapter.get("title", ""),
                    chapter["language"],
                    provider,
                    chapter.get("release_unit")
                    or (
                        "volume"
                        if chapter.get("volume") and not chapter.get("chapter")
                        else "chapter"
                    ),
                    json.dumps(chapter.get("groups", []), ensure_ascii=False),
                    chapter.get("publish_at"),
                    chapter.get("source_url", ""),
                    chapter.get("pages"),
                    chapter.get("version"),
                    str(library_path),
                    library_sha256,
                    content_sha256,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE chapter_release SET assembled_from=? WHERE id=?",
                (
                    json.dumps(provenance, ensure_ascii=False) if provenance else None,
                    chapter["id"],
                ),
            )
        return self.get_chapter(str(chapter["id"]))

    def chapter_archive_sha256(self, chapter_id: str) -> str | None:
        """Return the newest valid hash persisted by a completed import job."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT language_evidence_json
                FROM download_job
                WHERE chapter_id=?
                ORDER BY id DESC
                """,
                (chapter_id,),
            ).fetchall()
        for row in rows:
            try:
                evidence = json.loads(str(row["language_evidence_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            archive = evidence.get("archive") if isinstance(evidence, dict) else None
            digest = archive.get("sha256") if isinstance(archive, dict) else None
            if isinstance(digest, str) and self._valid_sha256(digest):
                return digest
        return None

    def list_chapter_job_paths(self, chapter_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, status, planned_path, result_path
                FROM download_job
                WHERE chapter_id=?
                ORDER BY id
                """,
                (chapter_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def chapter_job_paths_by_release(self) -> dict[str, list[dict[str, Any]]]:
        """Load every persisted library path once for bulk organization."""

        result: dict[str, list[dict[str, Any]]] = defaultdict(list)
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, chapter_id, status, planned_path, result_path
                FROM download_job
                WHERE planned_path IS NOT NULL OR result_path IS NOT NULL
                ORDER BY id
                """
            ).fetchall()
        for row in rows:
            result[str(row["chapter_id"])].append(dict(row))
        return result

    def record_chapter_library_sha256(
        self, chapter_id: str, expected_library_path: str, digest: str
    ) -> None:
        if not self._valid_sha256(digest):
            raise ValueError("Invalid chapter library SHA-256")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_release
                SET library_sha256=?, updated_at=?
                WHERE id=? AND downloaded=1 AND library_path=?
                  AND (library_sha256 IS NULL OR library_sha256=?)
                """,
                (digest, utc_now(), chapter_id, expected_library_path, digest),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Chapter library snapshot changed before hash persistence"
                )

    def relocate_chapter_library_file(
        self,
        chapter_id: str,
        expected_library_path: str,
        destination: Path,
        digest: str,
        job_path_updates: list[tuple[int, str, str]],
    ) -> None:
        """Commit one verified filesystem rename and all recorded path aliases."""

        if not self._valid_sha256(digest):
            raise ValueError("Invalid chapter library SHA-256")
        destination_text = str(destination)
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_release
                SET library_path=?, library_sha256=?, updated_at=?
                WHERE id=? AND downloaded=1 AND library_path=?
                  AND library_sha256=?
                """,
                (
                    destination_text,
                    digest,
                    utc_now(),
                    chapter_id,
                    expected_library_path,
                    digest,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Chapter library snapshot changed during file organization"
                )
            for job_id, field, expected_value in job_path_updates:
                if field not in {"planned_path", "result_path"}:
                    raise ValueError(f"Unsupported download job path field: {field}")
                updated = connection.execute(
                    f"""
                    UPDATE download_job SET {field}=?, updated_at=?
                    WHERE id=? AND chapter_id=? AND {field}=?
                    """,
                    (
                        destination_text,
                        utc_now(),
                        job_id,
                        chapter_id,
                        expected_value,
                    ),
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        f"Download job {job_id} {field} changed during organization"
                    )

    @staticmethod
    def _separate_numbered_prologue(chapter: sqlite3.Row) -> bool:
        return (
            numbered_prologue(chapter["title"]) is not None
            and chapter["canonical_chapter"] is None
            and chapter["numbering_method"] == "special_title"
            and chapter["numbering_status"] == "unmapped"
        )

    def create_job(
        self,
        manga_id: str,
        chapter_id: str,
        language: str,
        *,
        force: bool = False,
        origin: str = "",
    ) -> dict[str, Any]:
        """Queue one release. A blocked release needs ``force``.

        Blocking is the record of a release that already failed, so automatic
        selection must not queue it again: only an explicit operator retry
        (which clears the block) may.
        """

        now = utc_now()
        requested_origin = str(origin or "automatic").strip().casefold()
        queue_origin = (
            "manual"
            if requested_origin in {"manual", "manual retry"}
            else requested_origin
        )
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            chapter = connection.execute(
                """
                SELECT manga_id, release_unit, numbering_status, canonical_chapter,
                       title, numbering_method
                FROM chapter_release WHERE id=?
                """,
                (chapter_id,),
            ).fetchone()
            if chapter is None or str(chapter["manga_id"]) != manga_id:
                raise KeyError(chapter_id)
            # A numbered prelude has its own stable filename and identity. It
            # must never borrow the homonymous ordinary chapter's number,
            # but that deliberate separation does not make it undownloadable.
            separate_prologue = self._separate_numbered_prologue(chapter)
            if (
                not separate_prologue
                and str(chapter["release_unit"] or "chapter") == "chapter"
                and (
                    str(chapter["numbering_status"] or "mapped") != "mapped"
                    or chapter["canonical_chapter"] is None
                )
            ):
                raise ValueError(
                    "Release numbering is unresolved; map it to a canonical chapter first"
                )
            if not force:
                blocked = connection.execute(
                    "SELECT 1 FROM release_block WHERE chapter_id=?", (chapter_id,)
                ).fetchone()
                if blocked is not None:
                    raise ReleaseBlocked(chapter_id)
            active = connection.execute(
                JOB_SELECT
                + """
                WHERE job.chapter_id=?
                  AND job.status IN ('queued','running','downloading','packaging','importing')
                ORDER BY job.id DESC LIMIT 1
                """,
                (chapter_id,),
            ).fetchone()
            if active is not None:
                if queue_origin == "manual" and str(active["status"]) == "queued":
                    connection.execute(
                        "UPDATE download_job SET origin='manual', updated_at=? WHERE id=?",
                        (now, int(active["id"])),
                    )
                    active = connection.execute(
                        JOB_SELECT + " WHERE job.id=?", (int(active["id"]),)
                    ).fetchone()
                return self._decode_job(active)
            if origin:
                # Which path queued this: a wrong choice is otherwise only
                # visible afterwards, in the failure it produced.
                logger.info(
                    "Queueing %s of %s from %s (%s)",
                    chapter_id,
                    manga_id,
                    origin,
                    language,
                )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO download_job (
                    manga_id, chapter_id, requested_language, status, origin,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'queued', ?, ?, ?)
                """,
                (manga_id, chapter_id, language, queue_origin, now, now),
            )
            if cursor.rowcount:
                job_id = int(cursor.lastrowid)
            else:
                active = connection.execute(
                    JOB_SELECT
                    + """
                    WHERE job.chapter_id=?
                      AND job.status IN ('queued','running','downloading','packaging','importing')
                    ORDER BY job.id DESC LIMIT 1
                    """,
                    (chapter_id,),
                ).fetchone()
                if active is None:
                    raise RuntimeError("Unable to create or locate active download job")
                return self._decode_job(active)
        return self.get_job(job_id)

    def replan_queued_replacement(
        self,
        job_id: int,
        *,
        supersedes_chapter_id: str,
        expected_path: str,
        planned_path: Path,
    ) -> bool:
        """Move an unpublished legacy replacement off its old file's path.

        The service verifies the old file identity before this compare-and-set;
        a job already claimed or replanned by another worker is left untouched.
        """

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE download_job SET planned_path=?, updated_at=?
                WHERE id=? AND status='queued' AND planned_path=?
                  AND supersedes_chapter_id=?
                """,
                (
                    str(planned_path),
                    utc_now(),
                    job_id,
                    expected_path,
                    supersedes_chapter_id,
                ),
            )
            return cursor.rowcount == 1

    def claim_queued_job(
        self,
        job_id: int,
        planned_path: Path | None,
        *,
        acquisition_allowed: bool = True,
    ) -> dict[str, Any] | None:
        """Atomically claim one unsatisfied logical slot, exactly once."""

        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            target = connection.execute(
                """
                SELECT job.id AS job_id, job.status, job.next_retry_at,
                       chapter.id, chapter.manga_id, chapter.language,
                       chapter.volume, chapter.chapter, chapter.release_unit,
                       chapter.numbering_status, chapter.canonical_chapter,
                       chapter.title, chapter.numbering_method,
                       chapter.provider, chapter.source_url, chapter.source_name
                FROM download_job AS job
                JOIN chapter_release AS chapter ON chapter.id=job.chapter_id
                WHERE job.id=?
                """,
                (job_id,),
            ).fetchone()
            if target is None or str(target["status"]) != "queued":
                return None
            if float(target["next_retry_at"] or 0) > datetime.now(UTC).timestamp():
                return None
            from tankarr.source_circuit import source_gate_key

            gate = connection.execute(
                "SELECT next_retry_at FROM source_circuit WHERE source_key=?",
                (
                    source_gate_key(
                        target["provider"], target["source_url"], target["source_name"]
                    ),
                ),
            ).fetchone()
            if gate and float(gate[0]) > datetime.now(UTC).timestamp():
                return None
            if not acquisition_allowed:
                # Policy, publication status, or a manual edition total can
                # move after a job was queued.  Drop stale work at the same
                # atomic boundary that would otherwise start the download.
                connection.execute(
                    "DELETE FROM download_job WHERE id=? AND status='queued'",
                    (job_id,),
                )
                return None
            separate_prologue = self._separate_numbered_prologue(target)
            if (
                not separate_prologue
                and str(target["release_unit"] or "chapter") == "chapter"
                and (
                    str(target["numbering_status"] or "mapped") != "mapped"
                    or target["canonical_chapter"] is None
                )
            ):
                # Numbering evidence can change after a job was queued (for
                # example when an official source is identified).  Recheck at
                # the last atomic boundary before download so stale work can
                # never publish an unresolved release as a special/book file.
                connection.execute(
                    "DELETE FROM download_job WHERE id=? AND status='queued'",
                    (job_id,),
                )
                return None
            if planned_path is None:
                raise ValueError("A planned path is required for an eligible job")
            sibling_ids = (
                [str(target["id"])]
                if separate_prologue
                else self._download_slot_release_ids(connection, target)
            )
            placeholders = ",".join("?" for _ in sibling_ids)
            active_sibling = connection.execute(
                f"""
                SELECT 1
                FROM download_job
                WHERE id<>?
                  AND status IN ('running','downloading','packaging','importing')
                  AND chapter_id IN ({placeholders})
                LIMIT 1
                """,
                (job_id, *sibling_ids),
            ).fetchone()
            if active_sibling is not None:
                connection.execute(
                    "DELETE FROM download_job WHERE id=? AND status='queued'",
                    (job_id,),
                )
                return None
            cursor = connection.execute(
                """
                UPDATE download_job
                SET status='running', progress=0.01, message='Preparing download',
                    planned_path=COALESCE(planned_path, ?), updated_at=?
                WHERE id=? AND status='queued'
                """,
                (str(planned_path), utc_now(), job_id),
            )
            if cursor.rowcount == 0:
                return None
        return self.get_job(job_id)

    def get_job(self, job_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                JOB_SELECT + " WHERE job.id=?", (job_id,)
            ).fetchone()
        if row is None:
            raise KeyError(job_id)
        return self._decode_job(row)

    def list_jobs(
        self,
        limit: int = 100,
        statuses: Iterable[str] | None = None,
        manga_id: str | None = None,
    ) -> list[dict[str, Any]]:
        query = JOB_SELECT
        parameters: list[Any] = []
        selected = [status for status in statuses or [] if status]
        clauses: list[str] = []
        if selected:
            placeholders = ",".join("?" for _ in selected)
            clauses.append(f"job.status IN ({placeholders})")
            parameters.extend(selected)
        if manga_id:
            clauses.append("job.manga_id = ?")
            parameters.append(manga_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        active = {"queued", "running", "downloading", "packaging", "importing"}
        if selected and set(selected) <= active:
            query = QUEUE_CTE + query.replace(
                "FROM download_job AS job",
                "FROM download_job AS job LEFT JOIN queue_series ON queue_series.manga_id = job.manga_id",
                1,
            )
            query += f" ORDER BY {QUEUE_ORDER} LIMIT ?"
        else:
            query += " ORDER BY job.id DESC LIMIT ?"
        parameters.append(limit)
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode_job(row) for row in rows]

    def job_series_summary(self) -> list[dict[str, Any]]:
        """One row per series with active jobs, in download order: how many
        are queued and which chapter is being worked on right now."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT job.manga_id,
                       COALESCE(NULLIF(trim(manga.title_override), ''), NULLIF(trim(manga.metadata_title), ''), manga.title) AS manga_title,
                       COALESCE(
                           NULLIF(trim(manga.cover_url), ''),
                           CASE
                               WHEN NULLIF(trim(series_artwork.artwork_sha256), '')
                                    IS NOT NULL
                               THEN '/api/metadata/artwork/' || manga.id
                                    || '/series?v=' || series_artwork.artwork_sha256
                           END
                       ) AS manga_cover_url,
                       SUM(CASE WHEN job.status = 'queued' THEN 1 ELSE 0 END) AS queued,
                       SUM(CASE WHEN job.status != 'queued' THEN 1 ELSE 0 END) AS working,
                       MIN(CASE WHEN job.status = 'queued' THEN job.id END) AS first_queued_id,
                       MAX(CASE WHEN job.status != 'queued' THEN 1 ELSE 0 END) AS active_first
                FROM download_job job
                JOIN manga ON manga.id = job.manga_id
                LEFT JOIN series_metadata AS series_artwork
                  ON series_artwork.manga_id = manga.id
                WHERE job.status IN ('queued', 'running', 'downloading', 'packaging', 'importing')
                GROUP BY job.manga_id
                ORDER BY active_first DESC, queued, first_queued_id
                """
            ).fetchall()
            summary: list[dict[str, Any]] = []
            for row in rows:
                current = connection.execute(
                    JOB_SELECT
                    + " WHERE job.manga_id = ? AND job.status IN ('running','downloading','packaging','importing') ORDER BY job.id LIMIT 1",
                    (row["manga_id"],),
                ).fetchone()
                if current is None:
                    # A transient source retry is queued during its short
                    # backoff. Keep that state visible in Activity instead of
                    # making the series look idle between attempts.
                    current = connection.execute(
                        JOB_SELECT
                        + " WHERE job.manga_id = ? AND job.status = 'queued' "
                        "AND job.message LIKE 'Source unreachable (attempt %' "
                        "ORDER BY job.updated_at DESC LIMIT 1",
                        (row["manga_id"],),
                    ).fetchone()
                summary.append(
                    {
                        "manga_id": row["manga_id"],
                        "manga_title": row["manga_title"],
                        "manga_cover_url": row["manga_cover_url"],
                        "queued": int(row["queued"] or 0),
                        "working": int(row["working"] or 0),
                        "current": self._decode_job(current) if current else None,
                    }
                )
        return summary

    def job_status_counts(self) -> dict[str, int]:
        """How many download jobs sit in each status (the Activity header)."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM download_job GROUP BY status"
            ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def annotate_job(
        self,
        job_id: int,
        *,
        supersedes: str | None = None,
        quality_override: bool | None = None,
    ) -> dict[str, Any]:
        """Attach the deferred-replacement target and/or the operator override."""

        updates: list[str] = []
        values: list[Any] = []
        if supersedes is not None:
            updates.append("supersedes_chapter_id=?")
            values.append(str(supersedes) or None)
        if quality_override is not None:
            updates.append("quality_override=?")
            values.append(int(bool(quality_override)))
        if updates:
            with self.connect() as connection:
                connection.execute(
                    f"UPDATE download_job SET {', '.join(updates)}, updated_at=? WHERE id=?",
                    (*values, utc_now(), job_id),
                )
        return self.get_job(job_id)

    def retry_job(
        self, job_id: int, *, quality_override: bool = False
    ) -> dict[str, Any]:
        """Requeue one failed job so the worker can claim it again.

        ``quality_override`` is the operator saying "download it anyway":
        the length gate is waived for this one job (never the shape gate:
        un-sliced strips are unreadable whatever anyone decides).
        """

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE download_job
                SET status='queued', progress=0.0, message='Retry requested',
                    retry_count=0, next_retry_at=0, failure_code='', failure_scope='',
                    origin='manual', quality_override=?, updated_at=?
                WHERE id=? AND status='failed'
                """,
                (int(bool(quality_override)), utc_now(), job_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("Only failed jobs can be retried")
            # A manual retry is an explicit operator decision to trust this
            # release again, so it also clears the failover block.
            connection.execute(
                """
                DELETE FROM release_block
                WHERE chapter_id=(
                    SELECT chapter_id FROM download_job WHERE id=?
                )
                """,
                (job_id,),
            )
        return self.get_job(job_id)

    def reclassify_provider_releases_as_volumes(
        self,
        manga_id: str,
        provider: str,
        *,
        release_ids: Iterable[str] | None = None,
    ) -> int:
        """Turn one provider's chapter-numbered releases into whole volumes.

        The release identity is kept; only the unit changes. Library
        organization then renames the files from ``cNNN`` to ``vNNN``.
        """

        selected = tuple(release_ids) if release_ids is not None else None
        if selected == ():
            return 0
        scope = (
            " AND id IN (" + ",".join("?" for _ in selected) + ")"
            if selected is not None
            else ""
        )
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_release
                SET volume=chapter, chapter=NULL, release_unit='volume', updated_at=?
                WHERE manga_id=? AND provider=?
                  AND chapter IS NOT NULL AND (volume IS NULL OR volume='')
                  AND release_unit IN ('chapter', 'volume')
                """
                + scope,
                (utc_now(), manga_id, provider, *(selected or ())),
            )
        return cursor.rowcount

    def reclassify_mapped_ranges_as_volumes(
        self,
        manga_id: str,
        *,
        volume_count: int | None = None,
        chapter_total: int | None = None,
    ) -> list[str]:
        """Files named by chapter ranges that are the work's volumes become
        those volumes (Billy Bat: "c001-009" … "c158-165", 200 pages each).

        Two proofs are accepted: the range is a volume of the chapter map; or
        one source's ranges run contiguously from chapter 1 to the catalogue's
        chapter total in exactly as many files as the catalogue has volumes,
        in which case the k-th file is volume k. Returns the volumes assigned.
        """

        from tankarr.chapter_map import expand_numbers

        by_volume: dict[str, set[str]] = {}
        for entry in self.chapter_map(manga_id):
            volumes = tuple(entry.volumes)
            if len(volumes) != 1:
                continue
            by_volume.setdefault(str(volumes[0]), set()).update(
                str(c) for c in entry.chapters
            )
        ranged: list[tuple[int, int, dict[str, Any]]] = []
        for release in self.list_all_chapters(manga_id):
            if str(release.get("release_unit") or "chapter") == "volume":
                continue
            raw = str(release.get("chapter") or "").strip()
            if "-" not in raw:
                continue
            numbers = [
                int(float(c))
                for c in expand_numbers(raw)
                if str(c).replace(".", "").isdigit()
            ]
            if len(numbers) < 3:
                continue
            ranged.append((min(numbers), max(numbers), release))
        if not ranged:
            return []
        assigned: list[str] = []
        done: set[str] = set()
        for start, end, release in ranged:
            span = {str(n) for n in range(start, end + 1)}
            for volume, chapters in by_volume.items():
                if (
                    not chapters
                    or not span <= chapters
                    or len(span) < 0.8 * len(chapters)
                ):
                    continue
                if self.set_release_book(str(release["id"]), volume):
                    assigned.append(volume)
                    done.add(str(release["id"]))
                break
        # The sequence proof, per source: the range files, plus the books
        # that source already has, are exactly the catalogue's volumes; the
        # ranges run contiguously to the end and fill the free numbers in
        # order.
        if volume_count and chapter_total:
            by_source: dict[str, list[tuple[int, int, dict[str, Any]]]] = {}
            books_by_source: dict[str, set[int]] = {}
            for release in self.list_all_chapters(manga_id):
                key = f"{release.get('provider')}|{release.get('source_name') or ''}"
                if str(release.get("release_unit") or "chapter") == "volume":
                    try:
                        books_by_source.setdefault(key, set()).add(
                            int(float(str(release.get("volume") or "")))
                        )
                    except ValueError:
                        pass
            for start, end, release in ranged:
                if str(release["id"]) in done:
                    continue
                key = f"{release.get('provider')}|{release.get('source_name') or ''}"
                by_source.setdefault(key, []).append((start, end, release))
            for key, items in by_source.items():
                items.sort(key=lambda item: item[0])
                owned = {
                    n
                    for n in books_by_source.get(key, set())
                    if 1 <= n <= int(volume_count)
                }
                free = [n for n in range(1, int(volume_count) + 1) if n not in owned]
                if len(items) != len(free):
                    continue
                if items[-1][1] < int(chapter_total) - 1:
                    continue
                # Ranges must not overlap beyond a split chapter shared by two
                # books ("c086-094", "c094-101"); gaps are the books already
                # assigned above.
                ordered = all(
                    items[index + 1][0] >= items[index][1] - 1
                    for index in range(len(items) - 1)
                )
                if not ordered:
                    continue
                for position, (_start, _end, release) in zip(free, items):
                    if self.set_release_book(str(release["id"]), str(position)):
                        assigned.append(str(position))
                        done.add(str(release["id"]))
        return assigned

    def set_release_book(self, release_id: str, volume: str | None) -> bool:
        """Mark one release as a whole book (optionally numbered)."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_release
                SET release_unit='volume', volume=COALESCE(?, volume, chapter), chapter=NULL, updated_at=?
                WHERE id=? AND release_unit!='volume'
                """,
                (volume, utc_now(), release_id),
            )
            return cursor.rowcount > 0

    def add_match_review(
        self,
        manga_id: str,
        *,
        kind: str,
        provider: str,
        candidate_id: str,
        title: str,
        source_name: str | None = None,
        source_url: str | None = None,
        confidence: float = 0.0,
        reason: str = "",
        payload: dict[str, Any] | None = None,
        resolution: str | None = None,
    ) -> bool:
        """Record a near-miss as history; nothing is ever asked of a person.

        ``resolution`` is the decision the rules made. When the caller has
        none, the near-miss is refused: a match the automatic rules could not
        verify is not used, the same way Sonarr does not grab what its rules
        reject, and the row keeps the reason for the log and for the series
        page. Nothing reaches a confirmation list.
        """
        if resolution is None:
            resolution = REVIEW_REFUSED_BY_RULES

        with self.connect() as connection:
            existing = connection.execute(
                "SELECT id, resolution FROM match_review WHERE manga_id=? AND kind=? AND candidate_id=?",
                (manga_id, kind, candidate_id),
            ).fetchone()
            if existing is not None:
                return False
            connection.execute(
                """
                INSERT INTO match_review (manga_id, kind, provider, candidate_id, title, source_name, source_url, confidence, reason, payload_json, created_at, resolved_at, resolution)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manga_id,
                    kind,
                    provider,
                    candidate_id,
                    title[:300],
                    source_name,
                    source_url,
                    float(confidence or 0),
                    reason[:300],
                    json.dumps(payload or {}),
                    utc_now(),
                    utc_now() if resolution else None,
                    resolution,
                ),
            )
            return True

    def list_match_reviews(self, *, open_only: bool = True) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT r.*, COALESCE(NULLIF(trim(m.title_override), ''), NULLIF(trim(m.metadata_title), ''), m.title) AS manga_title,
                       m.cover_url AS manga_cover_url
                FROM match_review r JOIN manga m ON m.id = r.manga_id
                """
                + (" WHERE r.resolved_at IS NULL" if open_only else "")
                + " ORDER BY r.created_at DESC"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.pop("payload_json") or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            result.append(item)
        return result

    def resolve_match_review(self, review_id: int, resolution: str) -> dict[str, Any]:
        with self.connect() as connection:
            connection.execute(
                "UPDATE match_review SET resolved_at=?, resolution=? WHERE id=?",
                (utc_now(), resolution, review_id),
            )
            row = connection.execute(
                "SELECT * FROM match_review WHERE id=?", (review_id,)
            ).fetchone()
        if row is None:
            raise KeyError(review_id)
        item = dict(row)
        try:
            item["payload"] = json.loads(item.pop("payload_json") or "{}")
        except json.JSONDecodeError:
            item["payload"] = {}
        return item

    def record_wanted_attempt(
        self,
        manga_id: str,
        slot_key: str,
        *,
        channel: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        """Remember what one recovery channel answered for one wanted slot.

        Only the latest answer per channel is kept - an older "nobody has it"
        that a later pass disproved is not evidence of anything - alongside how
        many times the channel has been asked, which is what tells a slot
        nobody has ever scanned from one that failed once. A timeout is the
        one answer that says nothing: it never replaces a real verdict, or a
        slow indexer night would turn "not obtainable" back into "unknown".
        """

        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO wanted_attempt (
                    manga_id, slot_key, channel, outcome, detail,
                    attempts, first_attempted_at, attempted_at
                )
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(manga_id, slot_key, channel) DO UPDATE SET
                    outcome=CASE
                        WHEN excluded.outcome = 'error'
                             AND wanted_attempt.outcome NOT IN ('error', 'unavailable')
                        THEN wanted_attempt.outcome
                        ELSE excluded.outcome
                    END,
                    detail=CASE
                        WHEN excluded.outcome = 'error'
                             AND wanted_attempt.outcome NOT IN ('error', 'unavailable')
                        THEN wanted_attempt.detail
                        ELSE excluded.detail
                    END,
                    attempts=wanted_attempt.attempts + 1,
                    attempted_at=excluded.attempted_at
                """,
                (
                    manga_id,
                    str(slot_key),
                    str(channel),
                    str(outcome),
                    str(detail or "")[:500],
                    now,
                    now,
                ),
            )

    def wanted_attempts(self, manga_id: str) -> dict[str, list[dict[str, Any]]]:
        """Every recorded recovery attempt of one series, keyed by slot."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT slot_key, channel, outcome, detail, attempts,
                       first_attempted_at, attempted_at
                FROM wanted_attempt WHERE manga_id=?
                ORDER BY slot_key, attempted_at
                """,
                (manga_id,),
            ).fetchall()
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            grouped[str(row["slot_key"])].append(dict(row))
        return dict(grouped)

    def all_wanted_attempts(self) -> dict[str, dict[str, list[dict[str, Any]]]]:
        """The whole ledger in one query, keyed by series and then by slot.

        Wanted joins this onto every monitored series on every render; one
        query per series would put a hundred round trips on a page that is
        already the most expensive one Tankarr serves.
        """

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT manga_id, slot_key, channel, outcome, detail, attempts,
                       first_attempted_at, attempted_at
                FROM wanted_attempt ORDER BY manga_id, slot_key, attempted_at
                """
            ).fetchall()
        grouped: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for row in rows:
            record = dict(row)
            grouped[str(record.pop("manga_id"))][str(row["slot_key"])].append(record)
        return {key: dict(value) for key, value in grouped.items()}

    def forget_indexer_answers(self, manga_id: str) -> int:
        """Drop the indexers' "not offered"/"needs review" answers for a series.

        A person pressing Search is asking the indexers again, whatever they
        said last week; the rules that judged a release may have changed since.
        Settled and pending answers stay: a download on its way is not a
        question to repeat.
        """

        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM wanted_attempt WHERE manga_id=? AND channel IN "
                "('indexer_chapter', 'indexer_book') "
                "AND outcome IN ('not_offered', 'ambiguous', 'error', 'unavailable')",
                (manga_id,),
            )
            return int(cursor.rowcount or 0)

    def forget_wanted_attempts(self, manga_id: str, slot_keys: Iterable[str]) -> int:
        """Drop the ledger of slots that are no longer wanted."""

        keys = [str(key) for key in slot_keys]
        if not keys:
            return 0
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM wanted_attempt WHERE manga_id=? AND slot_key IN "
                f"({','.join('?' * len(keys))})",
                (manga_id, *keys),
            )
            return int(cursor.rowcount or 0)

    def record_page_quality(
        self,
        manga_id: str,
        chapter_id: str,
        *,
        verdict: str,
        assessment: dict[str, Any],
        library_sha256: str = "",
    ) -> None:
        """Remember how much page one imported chapter actually carries."""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO page_quality (
                    chapter_id, manga_id, verdict, normalized_height, pages,
                    measured_pages, median_aspect, max_width, median_width,
                    library_sha256, reason, measured_at, whole_chapter
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chapter_id) DO UPDATE SET
                    manga_id=excluded.manga_id,
                    verdict=excluded.verdict,
                    normalized_height=excluded.normalized_height,
                    pages=excluded.pages,
                    measured_pages=excluded.measured_pages,
                    median_aspect=excluded.median_aspect,
                    max_width=excluded.max_width,
                    median_width=excluded.median_width,
                    library_sha256=excluded.library_sha256,
                    reason=excluded.reason,
                    measured_at=excluded.measured_at,
                    whole_chapter=excluded.whole_chapter
                """,
                (
                    str(chapter_id),
                    manga_id,
                    str(verdict),
                    int(assessment.get("normalized_height") or 0),
                    int(assessment.get("pages") or 0),
                    int(assessment.get("measured_pages") or 0),
                    float(assessment.get("median_aspect") or 0.0),
                    int(assessment.get("max_width") or 0),
                    int(assessment.get("median_width") or 0),
                    str(library_sha256 or ""),
                    str(assessment.get("reason") or "")[:500],
                    utc_now(),
                    str(assessment.get("whole_chapter") or ""),
                ),
            )

    def book_page_signatures(self, library_sha256: str) -> list[int] | None:
        """The cached page signatures of one book file, by its content hash."""

        from tankarr.content_alignment import decode_signatures

        with self.connect() as connection:
            row = connection.execute(
                "SELECT signatures FROM book_page_signatures WHERE library_sha256=?",
                (str(library_sha256),),
            ).fetchone()
        return decode_signatures(row["signatures"]) if row else None

    def record_book_page_signatures(
        self, library_sha256: str, signatures: Iterable[int]
    ) -> None:
        from tankarr.content_alignment import encode_signatures

        values = list(signatures)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO book_page_signatures (
                    library_sha256, pages, signatures, computed_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(library_sha256) DO UPDATE SET
                    pages=excluded.pages,
                    signatures=excluded.signatures,
                    computed_at=excluded.computed_at
                """,
                (
                    str(library_sha256),
                    len(values),
                    encode_signatures(values),
                    utc_now(),
                ),
            )

    def record_content_alignment(
        self,
        manga_id: str,
        chapter_id: str,
        *,
        volume: str,
        alignment: dict[str, Any],
        chapter_sha256: str = "",
        book_sha256: str = "",
    ) -> None:
        """Remember whether a chapter file's pages were found inside a book."""

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chapter_content_alignment (
                    chapter_id, manga_id, volume, verdict, matched_pages, pages,
                    min_distance, chapter_sha256, book_sha256, checked_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chapter_id) DO UPDATE SET
                    manga_id=excluded.manga_id,
                    volume=excluded.volume,
                    verdict=excluded.verdict,
                    matched_pages=excluded.matched_pages,
                    pages=excluded.pages,
                    min_distance=excluded.min_distance,
                    chapter_sha256=excluded.chapter_sha256,
                    book_sha256=excluded.book_sha256,
                    checked_at=excluded.checked_at
                """,
                (
                    str(chapter_id),
                    manga_id,
                    str(volume or ""),
                    str(alignment.get("verdict") or ""),
                    int(alignment.get("matched_pages") or 0),
                    int(alignment.get("pages") or 0),
                    alignment.get("min_distance"),
                    str(chapter_sha256 or ""),
                    str(book_sha256 or ""),
                    utc_now(),
                ),
            )

    def content_alignment(self, manga_id: str) -> dict[str, dict[str, Any]]:
        """Every content verdict of one series, keyed by chapter id."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chapter_content_alignment WHERE manga_id=?",
                (manga_id,),
            ).fetchall()
        return {str(row["chapter_id"]): dict(row) for row in rows}

    def forget_content_alignment(self, chapter_ids: Iterable[str]) -> int:
        ids = [str(item) for item in chapter_ids]
        if not ids:
            return 0
        with self.connect() as connection:
            placeholders = ",".join("?" * len(ids))
            cursor = connection.execute(
                f"DELETE FROM chapter_content_alignment WHERE chapter_id IN ({placeholders})",
                ids,
            )
        return int(cursor.rowcount or 0)

    def manga_ids_with_books_and_chapters(self) -> list[str]:
        """Series holding at least one downloaded book and one chapter file:
        the only ones where a chapter can duplicate a book."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT manga_id FROM chapter_release
                WHERE downloaded=1
                GROUP BY manga_id
                HAVING SUM(CASE WHEN release_unit='volume' THEN 1 ELSE 0 END) > 0
                   AND SUM(CASE WHEN COALESCE(release_unit,'chapter')<>'volume'
                                THEN 1 ELSE 0 END) > 0
                ORDER BY manga_id
                """
            ).fetchall()
        return [str(row["manga_id"]) for row in rows]

    def page_quality(self, manga_id: str) -> dict[str, dict[str, Any]]:
        """Every measured chapter of one series, keyed by chapter id."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM page_quality WHERE manga_id=?", (manga_id,)
            ).fetchall()
        return {str(row["chapter_id"]): dict(row) for row in rows}

    def forget_page_quality(self, chapter_ids: Iterable[str]) -> int:
        """Drop measurements whose file is gone or has been replaced."""

        ids = [str(item) for item in chapter_ids]
        if not ids:
            return 0
        with self.connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM page_quality WHERE chapter_id IN ({','.join('?' * len(ids))})",
                tuple(ids),
            )
            return int(cursor.rowcount or 0)

    def record_indexer_offers(self, manga_id: str, offers: list[dict[str, Any]]) -> int:
        """Remember which books the indexers offer (coverage for the unit choice)."""

        now = utc_now()
        with self.connect() as connection:
            for offer in offers:
                connection.execute(
                    """
                    INSERT INTO indexer_offer (manga_id, volume, protocol, title, size_bytes, seen_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(manga_id, volume, protocol, title) DO UPDATE SET seen_at=excluded.seen_at, size_bytes=excluded.size_bytes
                    """,
                    (
                        manga_id,
                        str(offer["volume"]),
                        str(offer.get("protocol") or "torrent"),
                        str(offer.get("title") or "")[:300],
                        int(offer.get("size_bytes") or 0),
                        now,
                    ),
                )
        return len(offers)

    def list_indexer_offers(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT volume, protocol, title, size_bytes FROM indexer_offer "
                "WHERE manga_id=?",
                (manga_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_indexer_offers(self, manga_id: str, offers: list[dict[str, Any]]) -> int:
        """Retire cached book claims invalidated by the current identity policy."""

        if not offers:
            return 0
        with self.connect() as connection:
            deleted = 0
            for offer in offers:
                deleted += connection.execute(
                    "DELETE FROM indexer_offer WHERE manga_id=? AND volume=? "
                    "AND protocol=? AND title=?",
                    (
                        manga_id,
                        str(offer["volume"]),
                        str(offer["protocol"]),
                        str(offer["title"]),
                    ),
                ).rowcount
        return deleted

    def pending_book_volumes(
        self, manga_ids: Iterable[str] | None = None
    ) -> dict[str, set[int]]:
        """Books a grab is bringing in right now, per series, from the hint
        the indexer release carried ("7", "1-14"). Chapter grabs say nothing."""

        ids = [str(value) for value in (manga_ids or [])]
        query = (
            "SELECT manga_id, volume_hint FROM torrent_download "
            "WHERE status IN ('adding','queued','downloading','checking','completed','importing') "
            "AND volume_hint IS NOT NULL AND volume_hint <> ''"
        )
        params: tuple = ()
        if ids:
            query += f" AND manga_id IN ({','.join('?' * len(ids))})"
            params = tuple(ids)
        result: dict[str, set[int]] = {}
        with self.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        for row in rows:
            hint = str(row["volume_hint"] or "").strip()
            match = re.fullmatch(r"\s*(\d+)\s*(?:[-~]\s*(\d+))?\s*", hint)
            if not match:
                continue
            first = int(match.group(1))
            last = int(match.group(2) or first)
            if first < 1 or last < first or last - first > 200:
                continue
            result.setdefault(str(row["manga_id"]), set()).update(
                range(first, last + 1)
            )
        return result

    def list_indexer_volumes(self, manga_id: str) -> set[int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT volume FROM indexer_offer WHERE manga_id=?",
                (manga_id,),
            ).fetchall()
        result: set[int] = set()
        for row in rows:
            try:
                result.add(int(float(row["volume"])))
            except (TypeError, ValueError):
                continue
        return result

    def repair_reclassified_releases(self, manga_id: str) -> int:
        """Re-apply the volume unit to rows a provider refresh overwrote."""

        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE chapter_release
                SET volume=chapter, chapter=NULL, updated_at=?
                WHERE manga_id=? AND release_unit='volume'
                  AND chapter IS NOT NULL AND (volume IS NULL OR volume='')
                """,
                (utc_now(), manga_id),
            )
        return cursor.rowcount

    def restore_reclassified_chapters(self, chapters: dict[str, str]) -> int:
        """Restore explicit webtoon chapters that page count mistook for books."""

        rows = [
            (
                str(chapter),
                str(chapter),
                str(chapter),
                str(chapter),
                utc_now(),
                release_id,
            )
            for release_id, chapter in chapters.items()
            if str(release_id).strip() and str(chapter).strip()
        ]
        if not rows:
            return 0
        with self.connect() as connection:
            cursor = connection.executemany(
                """
                UPDATE chapter_release
                   SET volume=NULL, chapter=?, source_chapter=?, canonical_chapter=?,
                       primary_chapter=?, release_unit='chapter',
                       numbering_status='mapped', numbering_method='title_explicit',
                       numbering_confidence=1.0, updated_at=?
                 WHERE id=? AND release_unit='volume'
                """,
                rows,
            )
            return cursor.rowcount

    def replace_release_history(
        self, manga_id: str, source: str, releases: Iterable[dict[str, Any]]
    ) -> int:
        """Store one source's dated releases (the work's publishing rhythm)."""

        now = utc_now()
        rows = []
        for release in releases:
            chapter = str(release.get("chapter") or "").strip()
            released = str(release.get("release_date") or "").strip()
            if not chapter or not released:
                continue
            rows.append(
                (
                    manga_id,
                    source,
                    chapter,
                    str(release.get("volume") or ""),
                    released,
                    now,
                )
            )
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                "DELETE FROM series_release_history WHERE manga_id=? AND source=?",
                (manga_id, source),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO series_release_history (
                    manga_id, source, chapter, volume, release_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def list_release_history(self, manga_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT source, chapter, volume, release_date FROM series_release_history "
                "WHERE manga_id=? ORDER BY release_date",
                (manga_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def replace_chapter_map(
        self, manga_id: str, source: str, entries: Iterable[Any]
    ) -> int:
        """Store one source's chapter↔volume entries, replacing its previous set."""

        now = utc_now()
        rows = [
            (
                manga_id,
                source,
                ",".join(entry.volumes),
                ",".join(entry.chapters),
                int(entry.exact),
                entry.release_date,
                now,
            )
            for entry in entries
        ]
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            connection.execute(
                "DELETE FROM series_chapter_map WHERE manga_id=? AND source=?",
                (manga_id, source),
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO series_chapter_map (
                    manga_id, source, volumes, chapters, exact, release_date, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            # The map may make a decimal chapter canonical (And: "5.5" inside
            # volume 1): the releases' numbering must learn it at once.
            self._reconcile_numbering_connection(connection, manga_id)
        return len(rows)

    def chapter_map(self, manga_id: str, *, raw: bool = False) -> list[Any]:
        """Every known chapter↔volume entry for one series, exact ones first."""

        with self.connect() as connection:
            return self._chapter_map_connection(connection, manga_id, raw=raw)

    def _chapter_map_connection(
        self, connection: sqlite3.Connection, manga_id: str, *, raw: bool = False
    ) -> list[Any]:
        from tankarr.chapter_map import MapEntry, edition_entries, resolved_map

        rows = connection.execute(
            """
            SELECT cm.source, cm.volumes, cm.chapters, cm.exact, cm.release_date,
                   sm.data_json AS metadata_json
            FROM series_chapter_map cm
            LEFT JOIN series_metadata sm ON sm.manga_id=cm.manga_id
            WHERE cm.manga_id=?
            ORDER BY exact DESC, volumes, chapters
            """,
            (manga_id,),
        ).fetchall()
        entries = [
            MapEntry(
                volumes=tuple(str(row["volumes"]).split(",")),
                chapters=tuple(str(row["chapters"]).split(",")),
                exact=bool(row["exact"]),
                source=str(row["source"]),
                release_date=row["release_date"],
            )
            for row in rows
        ]
        if not raw and rows:
            entries = edition_entries(
                entries, json.loads(rows[0]["metadata_json"] or "{}")
            )
        return resolved_map(entries, raw=raw)

    # A source is tried behind the healthy ones after this many terminal
    # failures on the same work; one success clears the record.
    SOURCE_DEMOTION_FAILURES = 3
    # An engine restart must not condemn a good source, so transient errors
    # are counted apart and only mean something once they keep repeating:
    # "temporarily unavailable" ten times in a row is not temporary.
    TRANSIENT_DEMOTION_FAILURES = 10
    # Sources the runtime itself reports as unmaintained. They stay usable -
    # they may be the only place a work is published - but every maintained
    # source is tried first.
    deprioritised_sources: frozenset[str] = frozenset()

    def dismiss_alert(self, key: str, signature: str) -> None:
        """Hide one alert until what it reports changes.

        The signature is the alert's own content, so a dismissed "12 failed
        downloads" comes back as soon as the number moves: the operator
        acknowledged that situation, not the whole class of problem.
        """

        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO dismissed_alert (key, signature, dismissed_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    signature = excluded.signature,
                    dismissed_at = excluded.dismissed_at
                """,
                (str(key), str(signature), utc_now()),
            )

    def dismissed_alerts(self) -> dict[str, str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT key, signature FROM dismissed_alert"
            ).fetchall()
        return {str(row["key"]): str(row["signature"]) for row in rows}

    def failed_jobs_since(self, watermark: int) -> tuple[int, int]:
        """How many downloads failed after ``watermark``, and the newest id.

        Acknowledging "400 failed downloads" has to mean "I have seen these";
        counting them again the moment one more fails makes the dismissal
        useless, so the watermark is what was seen rather than the total.
        """

        # A download Tankarr itself refused because the pages were unreadable
        # is a verdict, not a failure: the series page and History show it,
        # and the failover has already moved on. It must not raise the alarm.
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS n,
                       COALESCE(MAX(id), 0) AS newest,
                       COALESCE(SUM(CASE WHEN id > ? THEN 1 ELSE 0 END), 0) AS fresh
                FROM download_job
                WHERE status='failed' AND message NOT LIKE ?
                """,
                (watermark, f"{QUALITY_REFUSAL_PREFIX}%"),
            ).fetchone()
        return int(row["fresh"]), int(row["newest"])

    def record_source_failure(
        self,
        manga_id: str,
        source_key: str,
        *,
        reason: str = "",
        transient: bool = False,
    ) -> int:
        """Count one failure of a source on a work.

        A transient failure (a restarting engine, a dropped connection) is
        counted apart: it never condemns a release, but a source that is
        "temporarily unavailable" over and over is simply broken.
        """

        key = str(source_key or "").casefold().strip()
        if not key:
            return 0
        # The perennial ranking counts every outcome, even for sources the
        # per-work bookkeeping ignores.
        self.record_source_health(key, ok=False, transient=transient, reason=reason)
        if key in self.deprioritised_sources:
            # The runtime already reports this extension as unmaintained, so
            # it is last everywhere: counting its failures work by work would
            # only duplicate a decision already taken.
            return 0
        column = "transient_failures" if transient else "failures"
        with self.connect() as connection:
            connection.execute(
                f"""
                INSERT INTO source_failure (
                    manga_id, source_key, {column}, last_reason, updated_at
                )
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(manga_id, source_key) DO UPDATE SET
                    {column} = {column} + 1,
                    last_reason = excluded.last_reason,
                    updated_at = excluded.updated_at
                """,
                (manga_id, key, str(reason)[:300], utc_now()),
            )
            row = connection.execute(
                f"SELECT {column} AS counted FROM source_failure "
                "WHERE manga_id=? AND source_key=?",
                (manga_id, key),
            ).fetchone()
        return int(row["counted"]) if row else 0

    def clear_source_failures(self, manga_id: str, source_key: str) -> bool:
        """One success proves the source works again on this work."""

        key = str(source_key or "").casefold().strip()
        if not key:
            return False
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM source_failure WHERE manga_id=? AND source_key=?",
                (manga_id, key),
            )
            return cursor.rowcount > 0

    # Perennial source ranking: cached briefly because Wanted evaluates
    # hundreds of releases per pass and the ledger only changes on outcomes.
    _health_cache: tuple[float, dict[str, int], frozenset[str]] | None = None

    def record_source_health(
        self,
        source_key: str,
        *,
        ok: bool,
        bytes_downloaded: int = 0,
        seconds: float = 0.0,
        transient: bool = False,
        reason: str = "",
    ) -> None:
        """Fold one download outcome into the perennial source ranking."""

        with self.connect() as connection:
            self._record_source_health_connection(
                connection,
                source_key,
                ok=ok,
                bytes_downloaded=bytes_downloaded,
                seconds=seconds,
                transient=transient,
                reason=reason,
            )

    def _record_source_health_connection(
        self,
        connection: sqlite3.Connection,
        source_key: str,
        *,
        ok: bool,
        bytes_downloaded: int = 0,
        seconds: float = 0.0,
        transient: bool = False,
        reason: str = "",
    ) -> None:
        key = str(source_key or "").casefold().strip()
        if not key:
            return
        row = connection.execute(
            "SELECT * FROM source_health WHERE source_key=?", (key,)
        ).fetchone()
        record = source_health.update(
            dict(row) if row else None,
            ok=ok,
            bytes_downloaded=bytes_downloaded,
            seconds=seconds,
            transient=transient,
        )
        connection.execute(
            """
            INSERT INTO source_health (
                source_key, attempts, failures, ema_success,
                ema_speed_bps, last_reason, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                attempts = excluded.attempts,
                failures = excluded.failures,
                ema_success = excluded.ema_success,
                ema_speed_bps = excluded.ema_speed_bps,
                last_reason = excluded.last_reason,
                updated_at = excluded.updated_at
            """,
            (
                key,
                record["attempts"],
                record["failures"],
                record["ema_success"],
                record["ema_speed_bps"],
                "" if ok else str(reason)[:300],
                record["updated_at"],
            ),
        )
        self._health_cache = None

    def source_health_rows(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM source_health").fetchall()
        return [dict(row) for row in rows]

    def _health_snapshot(self) -> tuple[dict[str, int], frozenset[str]]:
        cached = self._health_cache
        now = monotonic()
        if cached is not None and now - cached[0] < 30.0:
            return cached[1], cached[2]
        rows = self.source_health_rows()
        snapshot = (
            source_health.components(rows),
            source_health.unhealthy_keys(rows),
        )
        self._health_cache = (now, *snapshot)
        return snapshot

    def source_health_components(self) -> dict[str, int]:
        """source_key -> ranking component: the perennial standings."""

        return self._health_snapshot()[0]

    def global_demoted_sources(self) -> frozenset[str]:
        """Sources whose ledger says they keep failing, on every work."""

        return self._health_snapshot()[1]

    def demoted_sources(self, manga_id: str) -> frozenset[str]:
        """Sources tried only after the healthy ones for this work.

        Either they failed here too often, or the runtime reports the
        extension itself as unmaintained.
        """

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT source_key FROM source_failure "
                "WHERE manga_id=? AND (failures >= ? OR transient_failures >= ?)",
                (
                    manga_id,
                    self.SOURCE_DEMOTION_FAILURES,
                    self.TRANSIENT_DEMOTION_FAILURES,
                ),
            ).fetchall()
        return (
            frozenset(str(row["source_key"]) for row in rows)
            | self.deprioritised_sources
            | self.global_demoted_sources()
        )

    def block_release(self, chapter_id: str, *, reason: str) -> dict[str, Any] | None:
        """Exclude one failed release from automatic selection until unblocked."""

        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT manga_id FROM chapter_release WHERE id=?", (chapter_id,)
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                INSERT INTO release_block (chapter_id, manga_id, reason, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(chapter_id) DO UPDATE SET
                    reason=excluded.reason,
                    created_at=excluded.created_at
                """,
                (chapter_id, str(row["manga_id"]), reason[:1000], now),
            )
        return {
            "chapter_id": chapter_id,
            "manga_id": str(row["manga_id"]),
            "reason": reason[:1000],
            "created_at": now,
        }

    def unblock_release(self, chapter_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM release_block WHERE chapter_id=?", (chapter_id,)
            )
        return cursor.rowcount > 0

    def blocked_releases(self, manga_id: str) -> dict[str, dict[str, str]]:
        """Blocked release IDs for one series with their failure reasons."""

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT chapter_id, reason, created_at
                FROM release_block
                WHERE manga_id=?
                """,
                (manga_id,),
            ).fetchall()
        return {
            str(row["chapter_id"]): {
                "reason": str(row["reason"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        }

    def slot_release_ids(self, manga_id: str, chapter_id: str) -> list[str]:
        """Every release ID that satisfies the same logical slot as one release."""

        manga = self.get_manga(manga_id)
        chapters = self.list_chapters(manga_id, str(manga["preferred_language"]))
        volume_scoped = volume_scoped_numbering(chapters)
        target = next(
            (item for item in chapters if str(item["id"]) == chapter_id), None
        )
        if target is None:
            return []
        target_key = self._logical_chapter_key(target, volume_scoped=volume_scoped)
        return [
            str(item["id"])
            for item in chapters
            if self._logical_chapter_key(item, volume_scoped=volume_scoped)
            == target_key
        ]

    def _download_slot_release_ids(
        self, connection: sqlite3.Connection, target: sqlite3.Row
    ) -> list[str]:
        """Release IDs that represent the same downloadable chapter or volume."""

        target_data = dict(target)
        target_key = self._download_job_slot_key(target_data)
        rows = connection.execute(
            """
            SELECT id, volume, chapter
            FROM chapter_release
            WHERE manga_id=? AND language=?
            """,
            (str(target_data["manga_id"]), str(target_data["language"])),
        ).fetchall()
        return [
            str(row["id"])
            for row in rows
            if self._download_job_slot_key(dict(row)) == target_key
        ]

    def delete_job(self, job_id: int) -> dict[str, Any]:
        """Remove one job record that is not actively downloading."""

        job = self.get_job(job_id)
        if job["status"] in ACTIVE_DOWNLOAD_STATUSES:
            raise ValueError("An active job cannot be removed")
        with self.connect() as connection:
            connection.execute("DELETE FROM download_job WHERE id=?", (job_id,))
        return job

    def cancel_job(self, job_id: int) -> dict[str, Any]:
        """Atomically remove a queued or safely interrupted download job."""

        job = self.get_job(job_id)
        if job["status"] not in CANCELLABLE_DOWNLOAD_STATUSES:
            if job["status"] == "importing":
                raise ValueError(
                    "A job cannot be cancelled while its library import is being committed"
                )
            raise ValueError("This job is no longer active")
        with self.connect() as connection:
            placeholders = ",".join("?" for _ in CANCELLABLE_DOWNLOAD_STATUSES)
            cursor = connection.execute(
                f"DELETE FROM download_job WHERE id=? AND status IN ({placeholders})",
                (job_id, *CANCELLABLE_DOWNLOAD_STATUSES),
            )
            if cursor.rowcount != 1:
                raise ValueError("The job changed while cancellation was requested")
        return job

    def prune_duplicate_queued_jobs(self) -> int:
        """Drop queued jobs for logical slots already satisfied or in flight."""

        with self.connect() as connection:
            releases = connection.execute(
                """
                SELECT id, manga_id, language, volume, chapter, downloaded
                FROM chapter_release
                """
            ).fetchall()
            slot_by_release: dict[str, tuple[str, str, str, str]] = {}
            downloaded_release_ids: set[str] = set()
            satisfied_slots: set[tuple[str, str, str, str]] = set()
            for release in releases:
                release_data = dict(release)
                kind, label = self._download_job_slot_key(release_data)
                slot = (
                    str(release["manga_id"]),
                    str(release["language"]),
                    kind,
                    label,
                )
                slot_by_release[str(release["id"])] = slot
                if bool(release["downloaded"]):
                    downloaded_release_ids.add(str(release["id"]))
                    satisfied_slots.add(slot)

            jobs = connection.execute(
                """
                SELECT id, chapter_id, status, planned_path, supersedes_chapter_id
                FROM download_job
                WHERE status IN ('queued','running','downloading','packaging','importing')
                ORDER BY id
                """
            ).fetchall()
            # A job can outlive the release it points at: sources are
            # retired, releases past the official frontier are removed. Such a
            # job has nothing left to download, and letting it raise here
            # aborted startup before the worker ever began.
            orphaned = [
                int(job["id"])
                for job in jobs
                if str(job["chapter_id"]) not in slot_by_release
            ]
            if orphaned:
                connection.executemany(
                    "DELETE FROM download_job WHERE id=?",
                    [(job_id,) for job_id in orphaned],
                )
                jobs = [job for job in jobs if int(job["id"]) not in set(orphaned)]
            active_slots = {
                slot_by_release[str(job["chapter_id"])]
                for job in jobs
                if str(job["status"]) != "queued"
            }
            reserved_slots = satisfied_slots | active_slots
            recovery_slots: set[tuple[str, str, str, str]] = set()
            stale_job_ids: list[int] = []
            for job in jobs:
                if str(job["status"]) != "queued":
                    continue
                slot = slot_by_release[str(job["chapter_id"])]
                recovering_published_file = str(
                    job["chapter_id"]
                ) in downloaded_release_ids and bool(job["planned_path"])
                if (
                    (recovering_published_file or job["supersedes_chapter_id"])
                    and slot not in active_slots
                    and slot not in recovery_slots
                ):
                    recovery_slots.add(slot)
                    reserved_slots.add(slot)
                    continue
                if slot in reserved_slots:
                    stale_job_ids.append(int(job["id"]))
                    continue
                reserved_slots.add(slot)
            if not stale_job_ids:
                return 0
            placeholders = ",".join("?" for _ in stale_job_ids)
            cursor = connection.execute(
                f"DELETE FROM download_job WHERE status='queued' AND id IN ({placeholders})",
                stale_job_ids,
            )
            return int(cursor.rowcount or 0)

    def list_pending_jobs(self) -> list[dict[str, Any]]:
        statuses = ("queued", *ACTIVE_DOWNLOAD_STATUSES)
        placeholders = ",".join("?" for _ in statuses)
        with self.connect() as connection:
            rows = connection.execute(
                JOB_SELECT + f" WHERE job.status IN ({placeholders}) ORDER BY job.id",
                statuses,
            ).fetchall()
        return [self._decode_job(row) for row in rows]

    def has_pending_download_jobs(self) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM download_job "
                "WHERE status IN ('queued','running','downloading','packaging','importing') "
                "LIMIT 1"
            ).fetchone()
        return row is not None

    def list_queued_manga_ids(self) -> list[str]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT manga_id FROM download_job "
                "WHERE status='queued' ORDER BY manga_id"
            ).fetchall()
        return [str(row["manga_id"]) for row in rows]

    def queued_job_targets(self, manga_id: str) -> dict[int, str]:
        """Current release ID for every queued job in one series."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, chapter_id FROM download_job "
                "WHERE manga_id=? AND status='queued'",
                (manga_id,),
            ).fetchall()
        return {int(row["id"]): str(row["chapter_id"]) for row in rows}

    def manual_queued_job_ids(self, manga_id: str) -> set[int]:
        """Queued downloads the operator selected explicitly."""

        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id FROM download_job "
                "WHERE manga_id=? AND status='queued' AND origin='manual'",
                (manga_id,),
            ).fetchall()
        return {int(row["id"]) for row in rows}

    def retarget_queued_jobs(self, manga_id: str, replacements: dict[int, str]) -> int:
        """Move queued slots to their current preferred release atomically.

        Running jobs are deliberately excluded. The target must still belong
        to the same series and language and must not already be downloaded.
        """

        if not replacements:
            return 0
        changed = 0
        now = utc_now()
        with self.connect() as connection:
            for job_id, chapter_id in replacements.items():
                cursor = connection.execute(
                    """
                    UPDATE download_job
                    SET chapter_id=?, planned_path=NULL, progress=0,
                        message='Re-ranked to preferred source', updated_at=?
                    WHERE id=? AND manga_id=? AND status='queued'
                      AND chapter_id<>?
                      AND EXISTS (
                          SELECT 1 FROM chapter_release candidate
                          WHERE candidate.id=?
                            AND candidate.manga_id=download_job.manga_id
                            AND candidate.language=download_job.requested_language
                            AND candidate.downloaded=0
                      )
                    """,
                    (
                        chapter_id,
                        now,
                        int(job_id),
                        manga_id,
                        chapter_id,
                        chapter_id,
                    ),
                )
                changed += int(cursor.rowcount or 0)
        return changed

    def prune_recovered_failures(self) -> int:
        """Forget failed jobs whose chapter arrived from another source.

        A failure that the failover already repaired is history, not a
        problem: keeping it in the count hides the failures that still need
        someone. The row goes; the block on the failing release stays.
        """

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT job.id, job.manga_id, job.chapter_id
                FROM download_job AS job
                WHERE job.status = 'failed'
                """
            ).fetchall()
        recovered: list[int] = []
        for row in rows:
            try:
                slot = self.slot_release_ids(
                    str(row["manga_id"]), str(row["chapter_id"])
                )
            except KeyError:
                recovered.append(int(row["id"]))  # the series itself is gone
                continue
            if not slot:
                # The release no longer exists (its source was retired, or
                # the series re-indexed): measured live, 210 of 248 failures
                # were such ghosts, still counted as work for the operator.
                recovered.append(int(row["id"]))
                continue
            done = None
            if slot:
                placeholders = ",".join("?" for _ in slot)
                with self.connect() as connection:
                    done = connection.execute(
                        "SELECT 1 FROM chapter_release WHERE downloaded = 1 "
                        f"AND id IN ({placeholders}) LIMIT 1",
                        tuple(slot),
                    ).fetchone()
            if done is not None:
                recovered.append(int(row["id"]))
        if recovered:
            with self.connect() as connection:
                connection.executemany(
                    "DELETE FROM download_job WHERE id = ? AND status = 'failed'",
                    [(job_id,) for job_id in recovered],
                )
        return len(recovered)

    def list_queued_job_ids(self) -> list[int]:
        """Queued jobs in display order: smallest remaining series first.

        The worker does not consume this list serially; it uses
        ``list_queued_job_heads`` to distribute adaptive slots fairly.
        """

        with self.connect() as connection:
            rows = connection.execute(
                QUEUE_CTE + "SELECT job.id FROM download_job job "
                "LEFT JOIN queue_series ON queue_series.manga_id = job.manga_id "
                "LEFT JOIN chapter_release chapter ON chapter.id = job.chapter_id "
                f"WHERE job.status='queued' ORDER BY {QUEUE_ORDER}"
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def list_queued_job_heads(
        self, exclude_job_ids: Iterable[int] = ()
    ) -> list[dict[str, Any]]:
        """Return the next chapter of every queued series in queue order.

        The worker uses these heads to distribute live slots across series and
        source hosts before assigning a second slot to either. Keeping this as
        one window query avoids walking thousands of jobs in Python.
        """

        excluded = tuple(int(job_id) for job_id in exclude_job_ids)
        exclusion = (
            " AND job.id NOT IN (" + ",".join("?" for _ in excluded) + ")"
            if excluded
            else ""
        )
        with self.connect() as connection:
            rows = connection.execute(
                f"""
                WITH queue_series AS (
                    SELECT manga_id,
                           COUNT(*) AS queued_n,
                           MIN(id) AS first_queued_id
                    FROM download_job
                    WHERE status = 'queued'
                    GROUP BY manga_id
                ),
                ranked_jobs AS (
                    SELECT job.id,
                           job.manga_id,
                           chapter.provider AS chapter_provider,
                           chapter.source_name AS chapter_source_name,
                           chapter.source_url AS chapter_source_url,
                           queue_series.queued_n,
                           queue_series.first_queued_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY job.manga_id
                               ORDER BY CAST(chapter.chapter AS REAL), job.id
                           ) AS series_rank
                    FROM download_job AS job
                    JOIN queue_series ON queue_series.manga_id = job.manga_id
                    LEFT JOIN chapter_release AS chapter
                      ON chapter.id = job.chapter_id
                    WHERE job.status = 'queued'
                      AND job.next_retry_at <= unixepoch('now'){exclusion}
                      AND NOT EXISTS (
                          SELECT 1 FROM source_circuit gate
                          WHERE gate.source_key = source_gate_key(
                              chapter.provider, chapter.source_url, chapter.source_name)
                            AND gate.next_retry_at > unixepoch('now')
                      )
                )
                SELECT id,
                       manga_id,
                       chapter_provider,
                       chapter_source_name,
                       chapter_source_url,
                       queued_n,
                       first_queued_id
                FROM ranked_jobs
                WHERE series_rank = 1
                ORDER BY queued_n, first_queued_id, id
                """,
                excluded,
            ).fetchall()
        return [dict(row) for row in rows]

    def source_retry_at(self, source_key: str) -> float:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT next_retry_at FROM source_circuit WHERE source_key=?",
                (source_key,),
            ).fetchone()
        return float(row[0]) if row else 0.0

    def record_source_circuit(self, source_key: str, *, error: str = "") -> float:
        """A successful empty search is healthy; repeated failures pause one source."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not error:
                connection.execute(
                    "DELETE FROM source_circuit WHERE source_key=?", (source_key,)
                )
                return 0.0
            row = connection.execute(
                "SELECT failures FROM source_circuit WHERE source_key=?", (source_key,)
            ).fetchone()
            failures = int(row[0]) + 1 if row else 1
            delay = min(600, 30 * 2 ** min(5, failures - 2)) if failures > 1 else 0
            deadline = datetime.now(UTC).timestamp() + delay if delay else 0.0
            connection.execute(
                "INSERT INTO source_circuit VALUES (?, ?, ?, ?) "
                "ON CONFLICT(source_key) DO UPDATE SET failures=excluded.failures, "
                "next_retry_at=excluded.next_retry_at, failure_code=excluded.failure_code",
                (source_key, failures, deadline, error[:100]),
            )
        return deadline

    def update_job(
        self,
        job_id: int,
        *,
        status: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        result_path: Path | None = None,
        language_evidence: dict[str, Any] | None = None,
        retry_count: int | None = None,
        next_retry_at: float | None = None,
        failure_code: str | None = None,
        failure_scope: str | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"updated_at": utc_now()}
        for name, value in (
            ("retry_count", retry_count),
            ("next_retry_at", next_retry_at),
            ("failure_code", failure_code),
            ("failure_scope", failure_scope),
        ):
            if value is not None:
                updates[name] = value
        if status is not None:
            updates["status"] = status
        if progress is not None:
            updates["progress"] = max(0.0, min(1.0, progress))
        if message is not None:
            updates["message"] = message[:1000]
        if result_path is not None:
            updates["result_path"] = str(result_path)
        if language_evidence is not None:
            updates["language_evidence_json"] = json.dumps(
                language_evidence, ensure_ascii=False
            )
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE download_job SET {assignments} WHERE id=?",
                [*updates.values(), job_id],
            )
        return self.get_job(job_id)

    def create_torrent_download(
        self, manga_id: str, release: dict[str, Any]
    ) -> dict[str, Any]:
        now = utc_now()
        info_hash = str(release["info_hash"]).casefold()
        source = str(release.get("provider") or "").casefold()
        if source not in TORRENT_IMPORT_PROVIDERS:
            raise ValueError("Unsupported torrent release provider")
        with self.connect() as connection:
            self._require_manga(connection, manga_id)
            existing = connection.execute(
                TORRENT_SELECT + " WHERE torrent.info_hash=?", (info_hash,)
            ).fetchone()
            download_ref = str(
                release.get("download_ref") or release.get("torrent_url") or ""
            )
            if existing is not None:
                decoded = self._decode_torrent_download(existing)
                if (
                    decoded["manga_id"] != manga_id
                    or decoded["source"] != source
                    or decoded["source_id"] != str(release["id"])
                ):
                    raise ValueError(
                        "This torrent is already owned by a different Tankarr series"
                    )
                if decoded["status"] != "failed":
                    return decoded
                # A prior attempt died (a stale or missing download
                # reference, a transient client error); this caller has a
                # fresh reference for the same release, so retry it on the
                # same row instead of leaving a dead end permanently
                # occupying this info hash.
                connection.execute(
                    """
                    UPDATE torrent_download
                    SET title=?, language=?, category=?, indexer=?, size_bytes=?,
                        seeders=?, leechers=?, trusted=?, remake=?, volume_hint=?,
                        chapter_hint=?, publish_at=?, source_url=?, torrent_url=?,
                        status='adding', progress=0,
                        message=?, updated_at=?
                    WHERE id=?
                    """,
                    (
                        str(release["title"]),
                        str(release.get("language") or "en"),
                        str(release["category"]),
                        str(release.get("indexer") or "") or None,
                        int(release.get("size_bytes") or 0),
                        int(release.get("seeders") or 0),
                        int(release.get("leechers") or 0),
                        int(bool(release.get("trusted"))),
                        int(bool(release.get("remake"))),
                        release.get("volume"),
                        release.get("chapter"),
                        release.get("publish_at"),
                        str(release["source_url"]),
                        download_ref,
                        f"Validating {source.title()} torrent",
                        now,
                        decoded["id"],
                    ),
                )
                identifier = int(decoded["id"])
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO torrent_download (
                        manga_id, source, source_id, info_hash, title, language,
                        category, indexer, size_bytes, seeders, leechers, trusted, remake,
                        volume_hint, chapter_hint, publish_at, source_url,
                        torrent_url, status, progress, message, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'adding', 0, ?, ?, ?)
                    """,
                    (
                        manga_id,
                        source,
                        str(release["id"]),
                        info_hash,
                        str(release["title"]),
                        str(release.get("language") or "en"),
                        str(release["category"]),
                        str(release.get("indexer") or "") or None,
                        int(release.get("size_bytes") or 0),
                        int(release.get("seeders") or 0),
                        int(release.get("leechers") or 0),
                        int(bool(release.get("trusted"))),
                        int(bool(release.get("remake"))),
                        release.get("volume"),
                        release.get("chapter"),
                        release.get("publish_at"),
                        str(release["source_url"]),
                        download_ref,
                        f"Validating {source.title()} torrent",
                        now,
                        now,
                    ),
                )
                identifier = int(cursor.lastrowid)
        return self.get_torrent_download(identifier)

    def set_torrent_download_protocol(self, download_id: int, protocol: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE torrent_download SET protocol=? WHERE id=?",
                (
                    str(protocol).casefold()
                    if str(protocol).casefold() in {"usenet", "http"}
                    else "torrent",
                    download_id,
                ),
            )

    def get_torrent_download(self, download_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                TORRENT_SELECT + " WHERE torrent.id=?", (download_id,)
            ).fetchone()
        if row is None:
            raise KeyError(download_id)
        return self._decode_torrent_download(row)

    def get_torrent_download_by_hash(self, info_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                TORRENT_SELECT + " WHERE torrent.info_hash=?",
                (info_hash.casefold(),),
            ).fetchone()
        return self._decode_torrent_download(row) if row is not None else None

    def list_torrent_downloads(
        self,
        *,
        manga_id: str | None = None,
        statuses: Iterable[str] | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if manga_id is not None:
            clauses.append("torrent.manga_id=?")
            parameters.append(manga_id)
        selected = [status for status in statuses or [] if status in TORRENT_STATUSES]
        if selected:
            placeholders = ",".join("?" for _ in selected)
            clauses.append(f"torrent.status IN ({placeholders})")
            parameters.extend(selected)
        query = TORRENT_SELECT
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY torrent.id DESC LIMIT ?"
        parameters.append(max(1, min(int(limit), 500)))
        with self.connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._decode_torrent_download(row) for row in rows]

    def update_torrent_download(
        self,
        download_id: int,
        *,
        status: str | None = None,
        progress: float | None = None,
        qbit_state: str | None = None,
        content_path: str | Path | None = None,
        imported_paths: list[str] | None = None,
        language_evidence: dict[str, Any] | None = None,
        message: str | None = None,
        client_id: str | None = None,
        volume_hint: str | None = None,
        chapter_hint: str | None = None,
        selected_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        updates: dict[str, Any] = {"updated_at": utc_now()}
        if selected_paths is not None:
            updates["selected_paths_json"] = json.dumps(sorted(set(selected_paths)))
        if status is not None:
            if status not in TORRENT_STATUSES:
                raise ValueError(f"Unsupported torrent status: {status}")
            updates["status"] = status
        if progress is not None:
            updates["progress"] = max(0.0, min(1.0, float(progress)))
        if qbit_state is not None:
            updates["qbit_state"] = qbit_state[:100]
        if client_id is not None:
            updates["client_id"] = client_id[:100]
        if volume_hint is not None:
            updates["volume_hint"] = volume_hint[:32]
        if chapter_hint is not None:
            # The recovery ladder knows which slot it grabbed for; the name
            # parser only knows what the release is called. Telling the
            # importer the slot is what keeps a rescued chapter landing in it.
            updates["chapter_hint"] = chapter_hint[:32]
        if content_path is not None:
            updates["content_path"] = str(content_path)
        if imported_paths is not None:
            updates["imported_paths_json"] = json.dumps(
                imported_paths, ensure_ascii=False
            )
        if language_evidence is not None:
            updates["language_evidence_json"] = json.dumps(
                language_evidence, ensure_ascii=False
            )
        if message is not None:
            updates["message"] = message[:1000]
        assignments = ", ".join(f"{key}=?" for key in updates)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE torrent_download SET {assignments} WHERE id=?",
                [*updates.values(), download_id],
            )
            if cursor.rowcount != 1:
                raise KeyError(download_id)
        return self.get_torrent_download(download_id)

    def retry_torrent_download(self, download_id: int) -> dict[str, Any]:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE torrent_download
                SET status='queued', message='Retry requested', updated_at=?
                WHERE id=? AND status IN ('failed', 'review', 'completed')
                """,
                (utc_now(), download_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    "Only failed, review, or completed torrents can be retried"
                )
        return self.get_torrent_download(download_id)

    def delete_torrent_download(self, download_id: int) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM torrent_download WHERE id=?", (download_id,)
            )
            if cursor.rowcount != 1:
                raise KeyError(download_id)

    @staticmethod
    def _commit_deletion_operation(
        connection: sqlite3.Connection, operation_id: str | None
    ) -> None:
        if operation_id is None:
            return
        cursor = connection.execute(
            """
            UPDATE deletion_operation
            SET state='committed', updated_at=?
            WHERE id=? AND state='prepared'
            """,
            (utc_now(), operation_id),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"Deletion operation is missing or not prepared: {operation_id}"
            )

    @staticmethod
    def _valid_sha256(value: str) -> bool:
        return len(value) == 64 and all(
            character in "0123456789abcdef" for character in value
        )

    @staticmethod
    def _require_manga(connection: sqlite3.Connection, manga_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM manga WHERE id=?", (manga_id,)
        ).fetchone()
        if row is None:
            raise KeyError(manga_id)

    @staticmethod
    def _require_author(connection: sqlite3.Connection, author_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM author WHERE id=?", (author_id,)
        ).fetchone()
        if row is None:
            raise KeyError(author_id)
        return row

    @staticmethod
    def _require_chapter(
        connection: sqlite3.Connection, manga_id: str, chapter_id: str
    ) -> None:
        row = connection.execute(
            "SELECT 1 FROM chapter_release WHERE id=? AND manga_id=?",
            (chapter_id, manga_id),
        ).fetchone()
        if row is None:
            raise KeyError(chapter_id)

    @staticmethod
    def _raise_for_active_jobs(
        connection: sqlite3.Connection,
        predicate: str,
        parameters: tuple[Any, ...],
    ) -> None:
        placeholders = ",".join("?" for _ in ACTIVE_DOWNLOAD_STATUSES)
        rows = connection.execute(
            JOB_SELECT
            + f"""
            WHERE {predicate}
              AND job.status IN ({placeholders})
            ORDER BY job.id
            """,
            (*parameters, *ACTIVE_DOWNLOAD_STATUSES),
        ).fetchall()
        if rows:
            raise ActiveDownloadJobsError([Database._decode_job(row) for row in rows])

    @staticmethod
    def _raise_for_active_torrent_downloads(
        connection: sqlite3.Connection, manga_id: str
    ) -> None:
        # Terminal rows are download history, including completed direct/NZB
        # acquisitions. They cannot publish files and must not block removal.
        rows = connection.execute(
            """
            SELECT id, status FROM torrent_download
            WHERE manga_id=? AND status NOT IN ('imported', 'failed')
            ORDER BY id
            """,
            (manga_id,),
        ).fetchall()
        if rows:
            raise TorrentDownloadsExistError([dict(row) for row in rows])

    @staticmethod
    def _attach_logical_chapter_counts(
        connection: sqlite3.Connection, manga: list[dict[str, Any]]
    ) -> None:
        if not manga:
            return
        manga_by_id = {str(item["id"]): item for item in manga}
        placeholders = ",".join("?" for _item in manga_by_id)
        rows = connection.execute(
            f"""
            SELECT c.manga_id, c.chapter, c.downloaded
            FROM chapter_release c
            JOIN manga m ON m.id = c.manga_id
            WHERE c.manga_id IN ({placeholders})
              AND c.language = m.preferred_language
              AND NULLIF(trim(c.chapter), '') IS NOT NULL
            """,
            tuple(manga_by_id),
        ).fetchall()
        releases: dict[str, list[dict[str, Any]]] = {
            manga_id: [] for manga_id in manga_by_id
        }
        for row in rows:
            releases[str(row["manga_id"])].append(dict(row))
        for manga_id, item in manga_by_id.items():
            item.update(logical_chapter_coverage(releases[manga_id]))

    @staticmethod
    def _decode_author_row(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_works: bool = True,
    ) -> dict[str, Any]:
        result = dict(row)
        author_id = str(row["id"])
        alias_rows = connection.execute(
            """
            SELECT normalized_name, name, source_url, created_at, updated_at
            FROM author_alias
            WHERE author_id=?
            ORDER BY lower(name), normalized_name
            """,
            (author_id,),
        ).fetchall()
        aliases = [dict(alias) for alias in alias_rows]
        result["aliases"] = aliases
        result["source_urls"] = list(
            dict.fromkeys(
                str(alias["source_url"])
                for alias in alias_rows
                if str(alias["source_url"] or "").strip()
            )
        )
        work_rows = connection.execute(
            """
            SELECT author_work.catalogue_id, mangabaka_work.data_json
            FROM author_work
            JOIN mangabaka_work
              ON mangabaka_work.catalogue_id=author_work.catalogue_id
            WHERE author_work.author_id=?
            ORDER BY author_work.catalogue_id
            """,
            (author_id,),
        ).fetchall()
        result["work_ids"] = [str(work["catalogue_id"]) for work in work_rows]
        if include_works:
            result["works"] = [
                json.loads(work["data_json"] or "{}") for work in work_rows
            ]
        result["work_count"] = len(work_rows)
        result["library_manga_count"] = int(
            connection.execute(
                "SELECT COUNT(*) FROM manga_author WHERE author_id=?", (author_id,)
            ).fetchone()[0]
        )
        result["merged_from"] = [
            {
                "id": str(merged["id"]),
                "name": str(merged["display_name"]),
                "reason": str(merged["reason"]),
                "evidence": json.loads(merged["evidence_json"] or "{}"),
                "merged_at": str(merged["merged_at"]),
            }
            for merged in connection.execute(
                """
                SELECT source.id, source.display_name, author_merge.reason,
                       author_merge.evidence_json, author_merge.merged_at
                FROM author_merge
                JOIN author AS source ON source.id=author_merge.source_author_id
                WHERE author_merge.target_author_id=?
                ORDER BY author_merge.merged_at, source.id
                """,
                (author_id,),
            ).fetchall()
        ]
        result["refresh_failures"] = int(result.get("refresh_failures") or 0)
        return result

    @staticmethod
    def _decode_manga(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["translation_enabled"] = bool(result.get("translation_enabled", False))
        source_title = str(result.get("title") or "")
        metadata_title = str(result.get("metadata_title") or "").strip() or None
        title_override = str(result.get("title_override") or "").strip() or None
        result["source_title"] = source_title
        result["metadata_title"] = metadata_title
        result["title_override"] = title_override
        result["title"] = title_override or metadata_title or source_title
        result["title_source"] = (
            "manual" if title_override else "metadata" if metadata_title else "provider"
        )
        result["title_overridden"] = title_override is not None
        authors = json.loads(result.pop("authors_json", "[]"))
        raw_override = result.pop("authors_override_json", None)
        authors_override = json.loads(raw_override) if raw_override else None
        result["authors_override"] = authors_override
        result["authors_overridden"] = bool(authors_override)
        result["source_authors"] = authors
        result["authors"] = authors_override or authors
        result["creator_links"] = json.loads(result.pop("creator_links_json", "[]"))
        result["available_languages"] = json.loads(
            result.pop("available_languages_json", "[]")
        )
        result["external_correlations"] = json.loads(
            result.pop("external_correlations_json", "[]")
        )
        result["monitored"] = bool(result["monitored"])
        result["monitor_specials"] = bool(result.get("monitor_specials") or 0)
        result["auto_download"] = bool(result["auto_download"])
        result["assemble_books_automatically"] = bool(
            result.get("assemble_books_automatically", False)
        )
        result["monitor_initialized"] = bool(result["monitor_initialized"])
        result["future_monitoring_allowed"] = bool(result["future_monitoring_allowed"])
        result["chapter_count"] = int(result.get("chapter_count") or 0)
        result["downloaded_count"] = int(result.get("downloaded_count") or 0)
        result["numbered_chapter_count"] = int(
            result.get("numbered_chapter_count") or 0
        )
        result["numbered_volume_count"] = int(result.get("numbered_volume_count") or 0)
        result["status_override"] = (
            str(result.get("status_override") or "").strip() or None
        )
        result["status_overridden"] = result["status_override"] is not None
        result["library_status_override"] = (
            str(result.get("library_status_override") or "").strip() or None
        )
        result["library_status_overridden"] = (
            result["library_status_override"] is not None
        )
        result["expected_count_override"] = (
            int(result["expected_count_override"])
            if result.get("expected_count_override") is not None
            else None
        )
        result["expected_count_unit_override"] = (
            str(result.get("expected_count_unit_override") or "").strip() or None
        )
        return result

    @staticmethod
    def _decode_release_source(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["enabled"] = bool(result["enabled"])
        result["match_confidence"] = float(result["match_confidence"])
        return result

    @staticmethod
    def _decode_metadata_source_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = json.loads(result.pop("data_json", "{}"))
        result["raw"] = json.loads(result.pop("raw_json", "{}"))
        result["match_confidence"] = float(result["match_confidence"])
        return result

    @staticmethod
    def _decode_series_metadata_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        data = json.loads(result.pop("data_json", "{}"))
        result["source_status"] = json.loads(result.pop("source_status_json", "[]"))
        result["data"] = data
        return result

    @staticmethod
    def _decode_series_artwork_candidate_row(
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        result = dict(row)
        result["source_width"] = int(result["source_width"])
        result["source_height"] = int(result["source_height"])
        result["score"] = float(result["score"])
        result["source_priority"] = int(result["source_priority"])
        result["is_automatic"] = bool(result["is_automatic"])
        return result

    @staticmethod
    def _decode_volume_metadata_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["data"] = json.loads(result.pop("data_json", "{}"))
        return result

    @staticmethod
    def _decode_torrent_download(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["trusted"] = bool(result["trusted"])
        result["remake"] = bool(result["remake"])
        result["progress"] = float(result.get("progress") or 0)
        try:
            result["imported_paths"] = json.loads(
                result.pop("imported_paths_json", "[]")
            )
        except json.JSONDecodeError:
            result["imported_paths"] = []
        try:
            result["selected_paths"] = json.loads(
                result.pop("selected_paths_json", None) or "null"
            )
        except json.JSONDecodeError:
            result["selected_paths"] = None
        try:
            result["language_evidence"] = json.loads(
                result.pop("language_evidence_json", "{}")
            )
        except json.JSONDecodeError:
            result["language_evidence"] = {}
        return result

    @staticmethod
    def _logical_chapter_key(
        chapter: dict[str, Any], *, volume_scoped: bool = False
    ) -> tuple[str, str]:
        return logical_release_key(chapter, volume_scoped=volume_scoped)

    @staticmethod
    def _download_job_slot_key(release: dict[str, Any]) -> tuple[str, str]:
        """Stable queue identity shared by alternative provider releases."""

        chapter = canonical_number(release.get("chapter"))
        if chapter is not None:
            return ("chapter", chapter)
        volume = canonical_number(release.get("volume"))
        if volume is not None:
            return ("volume", volume)
        return ("id", str(release.get("id") or ""))

    @staticmethod
    def _volume_coverage(value: object) -> set[str]:
        raw = str(value or "").strip()
        if not raw:
            return set()
        match = re.fullmatch(r"0*(\d+)\s*[-–]\s*0*(\d+)", raw)
        if match is None:
            return {raw}
        start, end = (int(match.group(1)), int(match.group(2)))
        if start > end or end - start > 500:
            return {raw}
        return {str(number) for number in range(start, end + 1)}

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def _remeasure_page_quality_if_needed(connection: sqlite3.Connection) -> int:
        """Forget every measurement taken by an older method.

        The numbers changed meaning (spreads now count as two pages), so a
        row measured before and one measured after cannot share a baseline.
        The audit rebuilds them, 800 files a pass. Releases blocked only for
        being un-sliced strips are unblocked too: strips are now cut into
        pages at import instead of refused.
        """

        from tankarr.page_quality import MEASURE_VERSION

        row = connection.execute(
            "SELECT value FROM setting WHERE key=?", ("page_quality_measure_version",)
        ).fetchone()
        if row is not None and str(row["value"]) == MEASURE_VERSION:
            return 0
        forgotten = connection.execute("DELETE FROM page_quality").rowcount
        connection.execute(
            "DELETE FROM release_block WHERE reason LIKE '%taller than they are wide%'"
        )
        connection.execute(
            """
            INSERT INTO setting (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            ("page_quality_measure_version", MEASURE_VERSION, utc_now()),
        )
        return int(forgotten or 0)

    @staticmethod
    def _repair_year_fraction_labels(connection: sqlite3.Connection) -> int:
        """Undo volume/chapter labels that swallowed a year as a decimal.

        ``Tokyopop.Princess.Ai.Vol.03.2020`` was once parsed as volume
        ``3.202``: three books on disk named ``v001.202`` that cover none of
        the volumes the work is known to have. The parser now refuses a
        fractional part of three or more digits (no volume or chapter is
        numbered that finely); this puts the same rule to the rows written
        before it existed, and the organization pass then renames the files
        and their ComicInfo to match. Idempotent: a repaired label no longer
        matches.
        """

        repaired = 0
        for column in ("volume", "chapter"):
            rows = connection.execute(
                f"SELECT id, {column} FROM chapter_release "
                f"WHERE {column} GLOB '*.[0-9][0-9][0-9]*'"
            ).fetchall()
            for row in rows:
                raw = str(row[column])
                whole, _dot, fraction = raw.partition(".")
                if not fraction.isdigit() or len(fraction) < 3:
                    continue
                connection.execute(
                    f"UPDATE chapter_release SET {column}=? WHERE id=?",
                    (whole.lstrip("0") or "0", row["id"]),
                )
                repaired += 1
        return repaired

    def unobtainable_indexer_volumes(self, manga_id: str) -> set[int]:
        """Books the indexer hunt has already asked for and could not take.

        An indexer *offer* is recorded the moment a release is seen, before
        anyone checks whether it can be grabbed. When the hunt then refuses
        it - wrong work, single-word title nobody corroborates, dead swarm -
        the offer stays on file and keeps counting as "this book is
        available", which is how a series sits on the volume unit forever
        while the chapters that would complete it go unasked.
        """

        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT slot_key FROM wanted_attempt
                WHERE manga_id=? AND channel='indexer_book'
                  AND outcome IN ('not_offered', 'ambiguous', 'error')
                  AND slot_key LIKE 'volume:%'
                """,
                (manga_id,),
            ).fetchall()
        volumes: set[int] = set()
        for row in rows:
            label = str(row["slot_key"]).split(":", 1)[1]
            try:
                volumes.add(int(float(label)))
            except ValueError:
                continue
        return volumes

    @staticmethod
    def _migrate_download_job_table(connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='download_job'"
        ).fetchone()
        table_sql = str(row["sql"] or "") if row else ""
        normalized = "".join(table_sql.lower().split())
        if "unique(chapter_id,status)" not in normalized:
            return
        connection.execute(
            """
            CREATE TABLE download_job_migrated (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manga_id TEXT NOT NULL,
                chapter_id TEXT NOT NULL,
                requested_language TEXT NOT NULL,
                status TEXT NOT NULL,
                progress REAL NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                result_path TEXT,
                language_evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO download_job_migrated
            SELECT * FROM download_job
            """
        )
        connection.execute("DROP TABLE download_job")
        connection.execute("ALTER TABLE download_job_migrated RENAME TO download_job")

    @staticmethod
    def _decode_chapter(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["groups"] = json.loads(result.pop("groups_json", "[]"))
        from tankarr.assembly_provenance import assembly_provenance

        result["assembled_from"] = assembly_provenance(result.get("assembled_from"))
        result["numbering_evidence"] = json.loads(
            result.pop("numbering_evidence_json", "{}") or "{}"
        )
        canonical = result.get("canonical_chapter")
        if (
            str(result.get("release_unit") or "chapter") == "volume"
            or str(result.get("numbering_status") or "mapped") != "mapped"
        ):
            canonical = None
        result["chapter"] = canonical
        result["downloaded"] = bool(result["downloaded"])
        result["monitored"] = bool(result.get("monitored", 1))
        return result

    @staticmethod
    def _decode_job(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        source_retry = float(result.pop("source_retry_at", None) or 0)
        source_failure = result.pop("source_failure_code", None)
        if result.get("status") == "queued" and source_retry > float(
            result.get("next_retry_at") or 0
        ):
            result["next_retry_at"] = source_retry
            result["failure_scope"] = "source"
            result["failure_code"] = source_failure or "SourceUnavailable"
        result["language_evidence"] = json.loads(
            result.pop("language_evidence_json", "{}")
        )
        result["chapter_groups"] = json.loads(
            result.pop("chapter_groups_json", None) or "[]"
        )
        return result
