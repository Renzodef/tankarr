from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tankarr.download_sources import (
    DOWNLOAD_PROVIDER_PRIORITY_DEFAULT,
    normalize_provider_priority,
)
from tankarr.languages import (
    normalize_language_code,
    normalize_language_list,
    parse_language_list,
)
from tankarr.source_ranking import SourceRanking, normalize_source_list


def validate_extension_store_url(value: str | None) -> str:
    """An extension repository index: an https URL, or empty for none."""

    text = (value or "").strip()
    if not text:
        return ""
    parts = urlparse(text)
    if parts.scheme != "https" or not parts.netloc or "@" in parts.netloc:
        raise ValueError(
            "suwayomi_extension_store must be an https:// URL without credentials"
        )
    return text


def normalize_setup_completed_at(value: str) -> str:
    try:
        completed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "setup_completed_at must be an ISO timestamp with timezone"
        ) from exc
    if completed.utcoffset() is None:
        raise ValueError("setup_completed_at must include a timezone")
    return completed.astimezone(UTC).isoformat()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TANKARR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = 8787
    # Sub-path behind a reverse proxy ("/tankarr"); empty serves the root.
    url_base: str = ""
    data_dir: Path = Path("data")
    library_dir: Path = Path("data/library")
    import_dir: Path | None = None
    frontend_dir: Path | None = None
    default_language: str = "en"
    search_languages: str = "en"
    translation_enabled: bool = False
    translation_source_languages: str = "original"
    translation_processor_url: str | None = None
    translation_processor_token: str | None = Field(default=None, repr=False)
    translation_ai_url: str | None = None
    translation_ai_model: str = ""
    translation_ai_api_key: str | None = Field(default=None, repr=False)
    # Comma-separated host or container roots an earlier layout of this
    # installation recorded in the database (e.g. "/mnt/media/Comics"); files
    # found under them are resolved inside the current library directory.
    legacy_library_roots: str = ""
    provider_priority: str = ",".join(DOWNLOAD_PROVIDER_PRIORITY_DEFAULT)
    # Explicit source order per phase (comma-separated keys such as
    # "suwayomi:mangaplus,weebcentral"). Empty = class-based defaults;
    # backfill additionally inherits provider_priority.
    # Acquisition is applied only after releases have been mapped to the same
    # canonical chapter.  None keeps the legacy boolean/environment variable
    # working until the operator saves the new policy from Settings.
    release_acquisition_policy: (
        Literal["prefer_official", "first_available", "official_only"] | None
    ) = None
    prefer_official_releases: bool = True
    # Replacing an owned file consumes bandwidth and removes its provenance.
    # Keep this quality-upgrade pass opt-in; official sources are still
    # preferred for content that is genuinely missing.
    official_upgrade_enabled: bool = False
    source_upgrade_enabled: bool = False
    release_preference_profile: Literal["balanced", "official", "curated"] = "balanced"
    # Keep the library on the numbering of the official edition in the
    # operator's language: chapters past what the publisher has released are
    # chapters of another edition, and are removed. Only ever acts on an
    # enforceable frontier (see ``official_numbering``), and the download gate
    # already refuses to re-acquire past it, so the alignment converges.
    official_edition_alignment_enabled: bool = True
    # Measure imported chapters against the series' own page geometry and
    # replace the ones that carry a fraction of it, when another source has
    # the chapter. Never deletes without a replacement.
    page_quality_recovery_enabled: bool = True
    # Once a chapter's content is proven to sit inside a book Tankarr owns,
    # the chapter file is a duplicate: remove it on the monitor's cycle
    # instead of waiting for the operator to press the button. Files go
    # through the same quarantine as the manual deletion.
    duplicate_cleanup_enabled: bool = True
    # Unit preference when both chapters and books can complete a series.
    preferred_unit: Literal["chapters", "volumes"] = "volumes"
    # A special that no book contains: a half chapter, an omake, a second cut
    # of a story a source published apart. Off by default, because a shelf of
    # books should not carry rows for what was never bound into one.
    special_chapters_outside_books: bool = False
    source_priority_fresh: str = ""
    source_priority_backfill: str = ""
    suwayomi_enabled: bool = False
    # "managed": Tankarr installs and supervises the official Suwayomi-Server
    # JAR inside its own container (loopback only, no authentication).
    # "external": connect to a Suwayomi server you run yourself.
    suwayomi_mode: str = "managed"
    suwayomi_managed_heap_mb: int = Field(default=256, ge=128, le=1024)
    # Install the extension of a work's free official platform on its own
    # (managed runtime only), so official sources map by themselves.
    suwayomi_auto_install_official: bool = True
    # Index URL of the Mihon/Tachiyomi-compatible extension repository the
    # managed server installs extensions from. Tankarr ships none: the
    # operator chooses a repository, as Mihon and Suwayomi themselves do.
    suwayomi_extension_store: str = ""
    # Browser-facing address for "Open Suwayomi"; empty = derived from the
    # Tankarr host and port 4567, where the managed runtime serves its web UI.
    suwayomi_public_url: str | None = None
    suwayomi_url: str = "http://suwayomi:4567"
    suwayomi_username: str | None = None
    suwayomi_password: str | None = Field(default=None, repr=False)
    suwayomi_language: str = "en"
    suwayomi_source_ids: str = ""
    # Sources the operator installs are enabled, NSFW or not; the per-source
    # search budget is an internal constant, not a knob.
    suwayomi_search_timeout_seconds: float = Field(default=30.0, ge=2.0, le=120.0)
    prowlarr_enabled: bool = False
    prowlarr_url: str = "http://prowlarr:9696"
    prowlarr_api_key: str | None = Field(default=None, repr=False)
    prowlarr_indexer_ids: str = ""
    # archive.org as a direct-download book source (whole volumes only).
    internet_archive_enabled: bool = True
    prowlarr_categories: str = "7000,7020,7030"
    qbittorrent_url: str | None = None
    # The address a browser on the LAN can reach; qbittorrent_url is often a
    # Docker service name Tankarr resolves internally but a browser cannot.
    # Falls back to qbittorrent_url when unset.
    qbittorrent_public_url: str | None = None
    qbittorrent_username: str | None = None
    qbittorrent_password: str | None = Field(default=None, repr=False)
    qbittorrent_category: str = "tankarr"
    qbittorrent_save_path: str = "/data/downloads/tankarr"
    # Usenet client (SABnzbd): NZB releases found through Prowlarr. The
    # complete path is where SABnzbd itself writes; the download dir is the
    # same directory as Tankarr's container sees it.
    sabnzbd_url: str | None = None
    sabnzbd_public_url: str | None = None
    sabnzbd_api_key: str | None = Field(default=None, repr=False)
    sabnzbd_category: str = "tankarr"
    sabnzbd_complete_path: str = "/data/downloads/usenet"
    usenet_download_dir: Path | None = None
    torrent_download_dir: Path | None = None
    torrent_poll_interval_seconds: float = Field(default=10.0, ge=2.0, le=300.0)
    torrent_auto_import: bool = True
    # What happens to a torrent once its files are in the library:
    # seed (leave it to qBittorrent), remove_after_import, or
    # remove_when_seeded (qBittorrent pauses it at its ratio/time limits).
    torrent_completed_action: Literal[
        "seed", "remove_after_import", "remove_when_seeded"
    ] = "seed"
    # Torrents in Tankarr's category that no job references are removed
    # (with their files) once they are older than this.
    torrent_orphan_grace_hours: int = Field(default=24, ge=1, le=720)
    # A magnet whose metadata no peer delivers within this many minutes is
    # abandoned (removed from qBittorrent) so the next candidate release, a
    # usenet book for instance, gets its turn instead of the slot waiting on
    # a dead swarm forever.
    torrent_metadata_timeout_minutes: int = Field(default=30, ge=5, le=1440)
    request_timeout_seconds: float = 30.0
    download_concurrency: int = Field(default=12, ge=1, le=12)
    # Drain active chapters, retaining pending jobs across restarts. Local
    # imports and external translation processors continue independently.
    downloads_paused: bool = False
    # 0 delegates simultaneous chapter jobs entirely to the adaptive worker.
    # A positive value is an operator ceiling, never a forced concurrency.
    download_pipeline_max: int = Field(default=0, ge=0, le=64)
    import_max_expanded_bytes: int = Field(default=16 * 1024**3, ge=1024**2, le=1024**4)
    import_max_pages: int = Field(default=20000, ge=1, le=100000)
    import_subprocess_memory_mb: int = Field(default=1024, ge=128, le=16384)
    import_disk_reserve_bytes: int = Field(default=512 * 1024**2, ge=0, le=1024**4)
    backup_directory: Path | None = None
    backup_retention_count: int = Field(default=7, ge=1, le=365)
    recycle_bin_retention_days: int = Field(default=7, ge=1, le=365)
    setup_completed_at: str | None = None
    # Restored control-plane data must be inspected before any background I/O.
    restored_safe_mode: bool = False
    monitor_enabled: bool = True
    monitor_interval_seconds: float = Field(default=900.0, ge=5.0, le=86400.0)
    wanted_search_enabled: bool = True
    wanted_search_interval_seconds: float = Field(
        default=21600.0, ge=900.0, le=604800.0
    )
    # Series looked at per Wanted pass, most overdue first; the rest wait for
    # the next pass. Finished works back off between passes (search_cadence).
    wanted_search_budget: int = Field(default=25, ge=1, le=500)
    auth_method: str = "forms"
    auth_username: str | None = None
    auth_password: str | None = Field(default=None, repr=False)
    # Without a login, the first start creates one and prints it to the log.
    # false leaves an instance without credentials open: development only.
    auth_required: bool = True
    # Ask GitHub once a day whether a newer release exists (System page).
    update_check_enabled: bool = True
    ntfy_url: str | None = None
    ntfy_topic: str = "tankarr"
    ntfy_on_chapter_imported: bool = True
    ntfy_on_download_failed: bool = True
    # A human has to decide: a new match review, or a slot every channel
    # has given up on.
    ntfy_on_decision_needed: bool = True
    # Reader shortcut (independent of the optional Komga managed sync):
    # tankarr | auto | komga | kavita | stump | url | none.
    # The built-in reader needs no second catalogue or synchronization job.
    reader_kind: str = "auto"
    # auto follows series metadata/page shape; manga is paged RTL and webtoon
    # is a continuous vertical strip.
    reader_display_mode: str = "auto"
    reader_url: str | None = None
    reader_api_key: str | None = Field(default=None, repr=False)
    reader_username: str | None = None
    reader_password: str | None = Field(default=None, repr=False)
    # API route from inside the Tankarr container (Docker service name);
    # containers usually cannot hairpin to the host's LAN-bound port.
    reader_internal_url: str | None = None
    # Where the reader mounts Tankarr's library (Stump: series are looked up
    # by folder path, which is deterministic).
    reader_library_path: str = "/data/comics"
    reader_series_url_template: str | None = None
    komga_link_enabled: bool = False
    komga_url: str | None = None
    # Optional infrastructure-only route for Docker/service discovery. It is
    # intentionally not exposed in Settings; users configure one browser URL.
    komga_internal_url: str | None = None
    komga_library_id: str | None = None
    komga_auth_method: str = "auto"
    komga_api_key: str | None = Field(default=None, repr=False)
    komga_username: str | None = None
    komga_password: str | None = Field(default=None, repr=False)
    komga_reconcile_timeout_seconds: float = Field(default=600.0, ge=1.0, le=3600.0)
    # Queue-drain scans keep normal imports immediate. This slower full scan is
    # a safety net for filesystem drift and changes made outside Tankarr.
    komga_refresh_interval_minutes: int = Field(default=360, ge=15, le=10080)
    metadata_enabled: bool = True
    metadata_refresh_interval_hours: int = Field(default=168, ge=1, le=8760)
    author_refresh_interval_hours: int = Field(default=24, ge=1, le=720)
    mangabaka_api_url: str = "https://api.mangabaka.org/v1"
    mangaupdates_api_url: str = "https://api.mangaupdates.com/v1"
    anilist_api_url: str = "https://graphql.anilist.co"
    myanimelist_api_url: str = "https://api.myanimelist.net/v2"

    @field_validator("url_base")
    @classmethod
    def validate_url_base(cls, value: object) -> str:
        from tankarr.url_base import normalize_url_base

        return normalize_url_base(value)

    @field_validator("setup_completed_at")
    @classmethod
    def validate_setup_completed_at(cls, value: str | None) -> str | None:
        return normalize_setup_completed_at(value) if value else None

    @field_validator("default_language", "suwayomi_language")
    @classmethod
    def validate_language(cls, value: str) -> str:
        return normalize_language_code(value)

    @field_validator("search_languages")
    @classmethod
    def validate_search_languages(cls, value: str) -> str:
        return normalize_language_list(value)

    @field_validator("provider_priority")
    @classmethod
    def validate_provider_priority(cls, value: str) -> str:
        return normalize_provider_priority(value)

    @field_validator("source_priority_fresh", "source_priority_backfill")
    @classmethod
    def validate_source_priority(cls, value: str) -> str:
        return normalize_source_list(value)

    @property
    def source_ranking(self) -> SourceRanking:
        return SourceRanking.from_settings(
            fresh=self.source_priority_fresh,
            backfill=self.source_priority_backfill,
            provider_priority=self.provider_priority_order,
            acquisition_policy=self.effective_release_acquisition_policy,
            preference_profile=self.release_preference_profile,
        )

    @property
    def effective_release_acquisition_policy(self) -> str:
        if self.release_acquisition_policy:
            return self.release_acquisition_policy
        return "prefer_official" if self.prefer_official_releases else "first_available"

    @field_validator("suwayomi_mode")
    @classmethod
    def validate_suwayomi_mode(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"managed", "external"}:
            raise ValueError("TANKARR_SUWAYOMI_MODE must be 'managed' or 'external'")
        return normalized

    @field_validator("suwayomi_extension_store")
    @classmethod
    def validate_suwayomi_extension_store(cls, value: str) -> str:
        return validate_extension_store_url(value)

    @field_validator("suwayomi_public_url")
    @classmethod
    def validate_suwayomi_public_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return cls.validate_suwayomi_url(value)

    @field_validator("suwayomi_url")
    @classmethod
    def validate_suwayomi_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "TANKARR_SUWAYOMI_URL must be an HTTP(S) service URL without "
                "credentials, query, or fragment"
            )
        return normalized

    @field_validator("prowlarr_url")
    @classmethod
    def validate_prowlarr_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "TANKARR_PROWLARR_URL must be an HTTP(S) service root without "
                "credentials, path, query, or fragment"
            )
        return normalized

    @field_validator("qbittorrent_url", "qbittorrent_public_url")
    @classmethod
    def validate_qbittorrent_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "TANKARR_QBITTORRENT_URL must be an HTTP(S) service root without "
                "credentials, path, query, or fragment"
            )
        return normalized

    @field_validator("sabnzbd_public_url")
    @classmethod
    def validate_sabnzbd_public_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "TANKARR_SABNZBD_PUBLIC_URL must be an HTTP(S) service root without "
                "credentials, path, query, or fragment"
            )
        return normalized

    @field_validator("reader_kind")
    @classmethod
    def validate_reader_kind(cls, value: str) -> str:
        normalized = value.strip().casefold() or "auto"
        if normalized not in {
            "tankarr",
            "auto",
            "komga",
            "kavita",
            "stump",
            "url",
            "none",
        }:
            raise ValueError(
                "TANKARR_READER_KIND must be tankarr, auto, komga, kavita, stump, url or none"
            )
        return normalized

    @field_validator("reader_display_mode")
    @classmethod
    def validate_reader_display_mode(cls, value: str) -> str:
        normalized = value.strip().casefold() or "auto"
        if normalized not in {"auto", "manga", "webtoon"}:
            raise ValueError(
                "TANKARR_READER_DISPLAY_MODE must be auto, manga or webtoon"
            )
        return normalized

    @property
    def reader_api_url(self) -> str | None:
        return self.reader_internal_url or self.reader_url

    @field_validator("reader_url", "reader_internal_url")
    @classmethod
    def validate_reader_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("TANKARR_READER_URL must be an http(s) URL")
        return normalized

    @field_validator("reader_series_url_template")
    @classmethod
    def validate_reader_template(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        if not normalized.startswith(("http://", "https://")):
            raise ValueError(
                "The reader series URL template must start with http(s)://"
            )
        return normalized

    @field_validator("komga_url", "komga_internal_url")
    @classmethod
    def validate_komga_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Komga URLs must be HTTP(S) service roots without credentials, "
                "path, query, or fragment"
            )
        return normalized

    @field_validator("komga_library_id")
    @classmethod
    def validate_komga_library_id(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip()
        if len(normalized) > 100 or any(
            character.isspace() for character in normalized
        ):
            raise ValueError("TANKARR_KOMGA_LIBRARY_ID is invalid")
        return normalized

    @field_validator("komga_auth_method")
    @classmethod
    def validate_komga_auth_method(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"auto", "api_key", "basic"}:
            raise ValueError("TANKARR_KOMGA_AUTH_METHOD must be 'api_key' or 'basic'")
        return normalized

    @field_validator("qbittorrent_category")
    @classmethod
    def validate_qbittorrent_category(cls, value: str) -> str:
        normalized = value.strip()
        if (
            not normalized
            or len(normalized) > 50
            or not all(
                character.isalnum() or character in "._-" for character in normalized
            )
        ):
            raise ValueError(
                "TANKARR_QBITTORRENT_CATEGORY must contain only letters, numbers, "
                "dot, underscore, or dash"
            )
        return normalized

    @field_validator("qbittorrent_save_path")
    @classmethod
    def validate_qbittorrent_save_path(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        path = Path(normalized)
        if not normalized or not path.is_absolute() or ".." in path.parts:
            raise ValueError("TANKARR_QBITTORRENT_SAVE_PATH must be an absolute path")
        if normalized == "/":
            raise ValueError(
                "TANKARR_QBITTORRENT_SAVE_PATH cannot be the filesystem root"
            )
        return normalized

    @model_validator(mode="after")
    def validate_auth_credentials(self) -> Settings:
        if self.default_language not in self.search_language_codes:
            raise ValueError(
                "TANKARR_DEFAULT_LANGUAGE must also be enabled in "
                "TANKARR_SEARCH_LANGUAGES"
            )
        username_set = bool(self.auth_username)
        password_set = bool(self.auth_password)
        if username_set != password_set:
            raise ValueError(
                "TANKARR_AUTH_USERNAME and TANKARR_AUTH_PASSWORD must be set together"
            )
        if self.auth_username and ":" in self.auth_username:
            raise ValueError("TANKARR_AUTH_USERNAME cannot contain ':'")
        for name, value in (
            ("TANKARR_AUTH_USERNAME", self.auth_username),
            ("TANKARR_AUTH_PASSWORD", self.auth_password),
            ("TANKARR_SUWAYOMI_USERNAME", self.suwayomi_username),
            ("TANKARR_SUWAYOMI_PASSWORD", self.suwayomi_password),
            ("TANKARR_PROWLARR_API_KEY", self.prowlarr_api_key),
            ("TANKARR_QBITTORRENT_USERNAME", self.qbittorrent_username),
            ("TANKARR_QBITTORRENT_PASSWORD", self.qbittorrent_password),
            ("TANKARR_KOMGA_API_KEY", self.komga_api_key),
            ("TANKARR_KOMGA_USERNAME", self.komga_username),
            ("TANKARR_KOMGA_PASSWORD", self.komga_password),
        ):
            if value and ("\r" in value or "\n" in value):
                raise ValueError(f"{name} cannot contain line breaks")
        suwayomi_username_set = bool(self.suwayomi_username)
        suwayomi_password_set = bool(self.suwayomi_password)
        if suwayomi_username_set != suwayomi_password_set:
            raise ValueError(
                "TANKARR_SUWAYOMI_USERNAME and TANKARR_SUWAYOMI_PASSWORD must "
                "be set together"
            )
        qbittorrent_username_set = bool(self.qbittorrent_username)
        qbittorrent_password_set = bool(self.qbittorrent_password)
        if qbittorrent_username_set != qbittorrent_password_set:
            raise ValueError(
                "TANKARR_QBITTORRENT_USERNAME and TANKARR_QBITTORRENT_PASSWORD "
                "must be set together"
            )
        if self.qbittorrent_username and ":" in self.qbittorrent_username:
            raise ValueError("TANKARR_QBITTORRENT_USERNAME cannot contain ':'")
        komga_username_set = bool(self.komga_username)
        komga_password_set = bool(self.komga_password)
        if komga_username_set != komga_password_set:
            raise ValueError(
                "TANKARR_KOMGA_USERNAME and TANKARR_KOMGA_PASSWORD must be set together"
            )
        if self.komga_username and ":" in self.komga_username:
            raise ValueError("TANKARR_KOMGA_USERNAME cannot contain ':'")
        if self.komga_link_enabled and not self.komga_url:
            raise ValueError(
                "TANKARR_KOMGA_URL is required while the Komga shortcut is enabled"
            )
        if self.komga_link_enabled:
            if self.komga_effective_auth_method == "api_key" and not self.komga_api_key:
                raise ValueError(
                    "A Komga API key is required for the selected authentication method"
                )
            if self.komga_effective_auth_method == "basic" and not (
                self.komga_username and self.komga_password
            ):
                raise ValueError(
                    "A Komga username and password are required for the selected "
                    "authentication method"
                )
        invalid_source_ids = [
            item
            for item in self._csv(self.suwayomi_source_ids)
            if not item.isdecimal() or int(item) <= 0
        ]
        if invalid_source_ids:
            raise ValueError(
                "TANKARR_SUWAYOMI_SOURCE_IDS must contain positive numeric IDs"
            )
        for name, raw in (
            ("TANKARR_PROWLARR_INDEXER_IDS", self.prowlarr_indexer_ids),
            ("TANKARR_PROWLARR_CATEGORIES", self.prowlarr_categories),
        ):
            invalid_ids = [
                item
                for item in self._csv(raw)
                if not item.isdecimal() or int(item) <= 0
            ]
            if invalid_ids:
                raise ValueError(f"{name} must contain positive numeric IDs")
        return self

    @field_validator("auth_method")
    @classmethod
    def validate_auth_method(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"forms", "basic"}:
            raise ValueError("TANKARR_AUTH_METHOD must be 'forms' or 'basic'")
        return normalized

    @property
    def auth_configured(self) -> bool:
        return bool(self.auth_username and self.auth_password)

    @property
    def komga_effective_auth_method(self) -> str:
        """Resolve legacy configurations before an explicit UI choice is saved."""

        if self.komga_auth_method != "auto":
            return self.komga_auth_method
        if self.komga_api_key:
            return "api_key"
        if self.komga_username and self.komga_password:
            return "basic"
        return "api_key"

    @staticmethod
    def _csv(raw: str) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(item.strip() for item in raw.split(",") if item.strip())
        )

    @property
    def legacy_library_root_paths(self) -> tuple[Path, ...]:
        return tuple(Path(item) for item in self._csv(self.legacy_library_roots))

    @property
    def provider_priority_order(self) -> tuple[str, ...]:
        # The validator keeps the stored string canonical and total.
        return tuple(self.provider_priority.split(","))

    @property
    def suwayomi_managed(self) -> bool:
        return self.suwayomi_mode == "managed"

    @property
    def suwayomi_effective_url(self) -> str:
        """Where the Suwayomi GraphQL API actually lives for this mode."""

        if self.suwayomi_managed:
            return "http://127.0.0.1:4567"
        return self.suwayomi_url

    @property
    def suwayomi_effective_credentials(self) -> tuple[str | None, str | None]:
        if self.suwayomi_managed:
            # The managed server reuses Tankarr's own login so Settings can
            # link to its WebUI; without a Tankarr login it stays loopback-only.
            return self.suwayomi_managed_credentials or (None, None)
        return (self.suwayomi_username, self.suwayomi_password)

    @property
    def suwayomi_managed_credentials(self) -> tuple[str, str] | None:
        if self.auth_username and self.auth_password:
            return (self.auth_username, self.auth_password)
        return None

    @property
    def suwayomi_runtime_dir(self) -> Path:
        return self.data_dir / "suwayomi"

    @property
    def suwayomi_instance_token(self) -> str:
        """Token that namespaces every Suwayomi id Tankarr stores."""

        if self.suwayomi_managed:
            from tankarr.suwayomi_runtime import instance_token

            return instance_token(self.suwayomi_runtime_dir)
        return hashlib.sha1(self.suwayomi_url.encode()).hexdigest()[:8]

    @property
    def suwayomi_source_id_set(self) -> frozenset[int]:
        return frozenset(int(item) for item in self._csv(self.suwayomi_source_ids))

    @property
    def prowlarr_indexer_id_set(self) -> frozenset[int]:
        return frozenset(int(item) for item in self._csv(self.prowlarr_indexer_ids))

    @property
    def prowlarr_category_id_set(self) -> frozenset[int]:
        return frozenset(int(item) for item in self._csv(self.prowlarr_categories))

    @property
    def search_language_codes(self) -> tuple[str, ...]:
        return parse_language_list(self.search_languages)

    @property
    def search_language_set(self) -> frozenset[str]:
        return frozenset(self.search_language_codes)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "tankarr.sqlite3"

    @property
    def staging_dir(self) -> Path:
        return self.data_dir / "staging"

    @property
    def metadata_secrets_path(self) -> Path:
        return self.data_dir / "metadata.env"

    @property
    def import_operation_path(self) -> Path:
        return self.data_dir / "import-operation.json"

    def ensure_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)
        if not self.restored_safe_mode:
            self.library_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    environment = Settings()
    restored = environment.data_dir / "restored-settings.json"
    if not restored.exists():
        return environment
    if restored.is_symlink():
        raise ValueError("Restored settings must not be a symlink")
    values = json.loads(restored.read_text(encoding="utf-8"))
    # Explicit environment/CLI .env values retain their usual precedence;
    # missing configuration is reconstructed from the verified backup.
    values.update(
        {name: getattr(environment, name) for name in environment.model_fields_set}
    )
    values["data_dir"] = environment.data_dir
    return Settings.model_validate(values)
