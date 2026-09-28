from __future__ import annotations

import base64
import ipaddress
import json
import logging
import math
import os
import tempfile
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.parse import urlsplit

from tankarr.auth import forget_generated_login, generated_login_active
from tankarr.config import (
    Settings,
    normalize_setup_completed_at,
    validate_extension_store_url,
)
from tankarr.database import Database
from tankarr.download_sources import normalize_provider_priority
from tankarr.languages import normalize_language_code, normalize_language_list
from tankarr.source_ranking import normalize_acquisition_policy, normalize_source_list

logger = logging.getLogger(__name__)

SECRET_PLACEHOLDER = "••••••••"
SETTINGS_WRITE_LOCK = RLock()
SETTINGS_COMMIT_KEY = "__settings_commit_token"

MANAGED_SECRET_ENV: dict[str, str] = {
    "translation_ai_api_key": "TANKARR_TRANSLATION_AI_API_KEY",
    "translation_processor_token": "TANKARR_TRANSLATION_PROCESSOR_TOKEN",
    "auth_password": "TANKARR_AUTH_PASSWORD",
    "suwayomi_password": "TANKARR_SUWAYOMI_PASSWORD",
    "prowlarr_api_key": "TANKARR_PROWLARR_API_KEY",
    "qbittorrent_password": "TANKARR_QBITTORRENT_PASSWORD",
    "sabnzbd_api_key": "TANKARR_SABNZBD_API_KEY",
    "komga_api_key": "TANKARR_KOMGA_API_KEY",
    "reader_api_key": "TANKARR_READER_API_KEY",
    "reader_password": "TANKARR_READER_PASSWORD",
    "komga_password": "TANKARR_KOMGA_PASSWORD",
    "webhook_token": "TANKARR_WEBHOOK_TOKEN",
    "discord_webhook_url": "TANKARR_DISCORD_WEBHOOK_URL",
    "telegram_bot_token": "TANKARR_TELEGRAM_BOT_TOKEN",
    "apprise_urls": "TANKARR_APPRISE_URLS",
}


@dataclass(frozen=True)
class SettingSpec:
    kind: str  # "str" | "optional_str" | "float" | "int" | "bool"
    secret: bool = False
    minimum: float | None = None
    maximum: float | None = None
    min_length: int | None = None
    max_length: int | None = None


EDITABLE_SETTINGS: dict[str, SettingSpec] = {
    "translation_enabled": SettingSpec("bool"),
    "translation_source_languages": SettingSpec("str", min_length=2, max_length=200),
    "translation_processor_url": SettingSpec("optional_str", max_length=500),
    "translation_processor_token": SettingSpec(
        "optional_str", secret=True, max_length=500
    ),
    "translation_ai_url": SettingSpec("optional_str", max_length=500),
    "translation_ai_model": SettingSpec("str", max_length=200),
    "translation_ai_api_key": SettingSpec("optional_str", secret=True, max_length=500),
    "setup_completed_at": SettingSpec("optional_str", max_length=40),
    "backup_retention_count": SettingSpec("int", minimum=1, maximum=365),
    "recycle_bin_retention_days": SettingSpec("int", minimum=1, maximum=365),
    "auth_method": SettingSpec("str", min_length=5, max_length=5),
    "auth_username": SettingSpec("optional_str", min_length=1, max_length=200),
    "auth_password": SettingSpec(
        "optional_str", secret=True, min_length=8, max_length=300
    ),
    "default_language": SettingSpec("str", min_length=2, max_length=16),
    "search_languages": SettingSpec("str", min_length=2, max_length=500),
    "monitor_interval_seconds": SettingSpec("float", minimum=60, maximum=86400),
    "monitor_enabled": SettingSpec("bool"),
    "wanted_search_enabled": SettingSpec("bool"),
    "wanted_search_interval_seconds": SettingSpec("float", minimum=900, maximum=604800),
    "download_concurrency": SettingSpec("int", minimum=1, maximum=12),
    "download_pipeline_max": SettingSpec("int", minimum=0, maximum=64),
    "downloads_paused": SettingSpec("bool"),
    "import_max_expanded_bytes": SettingSpec("int", minimum=1024**2, maximum=1024**4),
    "import_max_pages": SettingSpec("int", minimum=1, maximum=100000),
    "import_subprocess_memory_mb": SettingSpec("int", minimum=128, maximum=16384),
    "import_disk_reserve_bytes": SettingSpec("int", minimum=0, maximum=1024**4),
    "provider_priority": SettingSpec("str", min_length=3, max_length=200),
    "release_acquisition_policy": SettingSpec("str", min_length=13, max_length=16),
    "official_upgrade_enabled": SettingSpec("bool"),
    "source_upgrade_enabled": SettingSpec("bool"),
    "release_preference_profile": SettingSpec("str", max_length=20),
    "official_edition_alignment_enabled": SettingSpec("bool"),
    "page_quality_recovery_enabled": SettingSpec("bool"),
    "duplicate_cleanup_enabled": SettingSpec("bool"),
    "preferred_unit": SettingSpec("str", min_length=7, max_length=8),
    "special_chapters_outside_books": SettingSpec("bool"),
    "source_priority_fresh": SettingSpec("str", max_length=2_000),
    "source_priority_backfill": SettingSpec("str", max_length=2_000),
    "suwayomi_enabled": SettingSpec("bool"),
    "suwayomi_mode": SettingSpec("str", min_length=7, max_length=8),
    "suwayomi_managed_heap_mb": SettingSpec("int", minimum=128, maximum=1024),
    "suwayomi_public_url": SettingSpec("optional_str", max_length=300),
    "suwayomi_url": SettingSpec("str", min_length=8, max_length=300),
    "suwayomi_username": SettingSpec("optional_str", max_length=200),
    "suwayomi_password": SettingSpec("optional_str", secret=True, max_length=300),
    "suwayomi_language": SettingSpec("str", min_length=2, max_length=16),
    "suwayomi_source_ids": SettingSpec("str", max_length=2_000),
    "prowlarr_enabled": SettingSpec("bool"),
    "prowlarr_url": SettingSpec("str", min_length=8, max_length=300),
    "prowlarr_api_key": SettingSpec("optional_str", secret=True, max_length=300),
    "prowlarr_indexer_ids": SettingSpec("str", max_length=2_000),
    "internet_archive_enabled": SettingSpec("bool"),
    "prowlarr_categories": SettingSpec("str", min_length=1, max_length=2_000),
    "torrent_auto_import": SettingSpec("bool"),
    "qbittorrent_url": SettingSpec("optional_str", max_length=300),
    "qbittorrent_username": SettingSpec("optional_str", max_length=200),
    "qbittorrent_password": SettingSpec("optional_str", secret=True, max_length=300),
    "qbittorrent_category": SettingSpec("str", min_length=1, max_length=50),
    "torrent_completed_action": SettingSpec("str", min_length=4, max_length=24),
    "suwayomi_auto_install_official": SettingSpec("bool"),
    "suwayomi_extension_store": SettingSpec("str", max_length=500),
    "sabnzbd_url": SettingSpec("optional_str", max_length=300),
    "sabnzbd_api_key": SettingSpec("optional_str", secret=True, max_length=300),
    "sabnzbd_category": SettingSpec("str", min_length=1, max_length=50),
    "sabnzbd_complete_path": SettingSpec("str", min_length=1, max_length=300),
    "torrent_orphan_grace_hours": SettingSpec("int", minimum=1, maximum=720),
    "reader_kind": SettingSpec("str", min_length=3, max_length=8),
    "reader_display_mode": SettingSpec("str", min_length=4, max_length=7),
    "reader_url": SettingSpec("optional_str", max_length=300),
    "reader_api_key": SettingSpec("optional_str", secret=True, max_length=300),
    "reader_username": SettingSpec("optional_str", max_length=200),
    "reader_internal_url": SettingSpec("optional_str", max_length=300),
    "reader_password": SettingSpec("optional_str", secret=True, max_length=300),
    "reader_library_path": SettingSpec("str", min_length=1, max_length=300),
    "reader_series_url_template": SettingSpec("optional_str", max_length=600),
    "komga_link_enabled": SettingSpec("bool"),
    "komga_url": SettingSpec("optional_str", max_length=300),
    "komga_library_id": SettingSpec("optional_str", max_length=100),
    "komga_auth_method": SettingSpec("str", min_length=4, max_length=7),
    "komga_api_key": SettingSpec("optional_str", secret=True, max_length=500),
    "komga_username": SettingSpec("optional_str", max_length=200),
    "komga_password": SettingSpec("optional_str", secret=True, max_length=300),
    "komga_refresh_interval_minutes": SettingSpec("int", minimum=15, maximum=10080),
    "metadata_enabled": SettingSpec("bool"),
    "metadata_refresh_interval_hours": SettingSpec("int", minimum=1, maximum=8760),
    "ntfy_url": SettingSpec("optional_str", max_length=300),
    "ntfy_topic": SettingSpec("str", min_length=1, max_length=100),
    "ntfy_on_chapter_imported": SettingSpec("bool"),
    "ntfy_on_download_failed": SettingSpec("bool"),
    "ntfy_on_decision_needed": SettingSpec("bool"),
    "webhook_url": SettingSpec("optional_str", max_length=500),
    "webhook_token": SettingSpec("optional_str", secret=True, max_length=500),
    "discord_webhook_url": SettingSpec("optional_str", secret=True, max_length=500),
    "telegram_bot_token": SettingSpec("optional_str", secret=True, max_length=200),
    "telegram_chat_id": SettingSpec("optional_str", max_length=100),
    "apprise_url": SettingSpec("optional_str", max_length=500),
    "apprise_key": SettingSpec("optional_str", max_length=200),
    "apprise_urls": SettingSpec("optional_str", secret=True, max_length=2_000),
}


def coerce_setting(name: str, raw: str) -> Any:
    spec = EDITABLE_SETTINGS[name]
    value = raw.strip()
    if spec.kind in {"optional_str"} and value == "":
        return None
    if spec.kind in {"str", "optional_str"}:
        if spec.secret and ("\r" in value or "\n" in value):
            raise ValueError(f"{name} cannot contain line breaks")
        if spec.min_length is not None and len(value) < spec.min_length:
            raise ValueError(f"{name} must be at least {spec.min_length} characters")
        if spec.max_length is not None and len(value) > spec.max_length:
            raise ValueError(f"{name} must be at most {spec.max_length} characters")
        if name == "setup_completed_at":
            return normalize_setup_completed_at(value)
        if name == "auth_method" and value not in {"forms", "basic"}:
            raise ValueError("auth_method must be forms or basic")
        if name == "komga_auth_method" and value not in {"auto", "api_key", "basic"}:
            raise ValueError("komga_auth_method must be api_key or basic")
        if name in {"default_language", "suwayomi_language"}:
            return normalize_language_code(value)
        if name == "search_languages":
            return normalize_language_list(value)
        if name == "translation_source_languages":
            from tankarr.translation_policy import normalize_fallback_languages

            return normalize_fallback_languages(value)
        if name in {"translation_processor_url", "translation_ai_url"}:
            from tankarr.translation_policy import validate_translation_url

            return validate_translation_url(value)
        if name == "provider_priority":
            return normalize_provider_priority(value)
        if name in {"source_priority_fresh", "source_priority_backfill"}:
            return normalize_source_list(value)
        if name == "release_preference_profile" and value not in {
            "balanced",
            "official",
            "curated",
        }:
            raise ValueError(
                "release_preference_profile must be balanced, official or curated"
            )
        if name == "release_acquisition_policy":
            return normalize_acquisition_policy(value)
        if (
            name == "auth_username"
            and value
            and (":" in value or "\r" in value or "\n" in value)
        ):
            raise ValueError("auth_username cannot contain colon or line breaks")
        if (
            name
            in {
                "prowlarr_url",
                "qbittorrent_url",
                "suwayomi_url",
            }
            and value
        ):
            _validate_internal_service_url(name, value)
        if name == "komga_url" and value:
            _validate_komga_service_url(name, value)
        if name == "suwayomi_extension_store":
            return validate_extension_store_url(value)
        if (
            name == "qbittorrent_category"
            and value
            and not all(
                character.isalnum() or character in "._-" for character in value
            )
        ):
            raise ValueError(
                "qbittorrent_category may contain only letters, numbers, dot, "
                "underscore, or dash"
            )
        return value
    if spec.kind == "bool":
        normalized = value.casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{name} must be true or false")
    try:
        if spec.kind == "float":
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"{name} must be a finite number")
        else:
            exact = Decimal(value)
            if not exact.is_finite():
                raise ValueError(f"{name} must be a finite number")
            if exact != exact.to_integral_value():
                raise ValueError(f"{name} must be a whole number")
            # Check the bounded decimal before conversion to a Python int.
            # This also rejects enormous exponents without allocating them.
            if spec.minimum is not None and exact < spec.minimum:
                raise ValueError(f"{name} must be at least {spec.minimum:g}")
            if spec.maximum is not None and exact > spec.maximum:
                raise ValueError(f"{name} must be at most {spec.maximum:g}")
            number = int(exact)
    except (InvalidOperation, OverflowError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if spec.minimum is not None and number < spec.minimum:
        raise ValueError(f"{name} must be at least {spec.minimum:g}")
    if spec.maximum is not None and number > spec.maximum:
        raise ValueError(f"{name} must be at most {spec.maximum:g}")
    return number


def _validate_internal_service_url(name: str, value: str) -> None:
    """Keep credential-bearing service probes on the trusted local network."""

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            f"{name} must be a local HTTP(S) service root without credentials, "
            "path, query, or fragment"
        )

    hostname = parsed.hostname.casefold().rstrip(".")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        trusted_name = (
            hostname == "localhost"
            or "." not in hostname
            or hostname.endswith((".local", ".lan", ".home.arpa"))
        )
        if not trusted_name:
            raise ValueError(f"{name} must target a local service hostname") from None
        return

    carrier_grade_nat = address in ipaddress.ip_network("100.64.0.0/10")
    if (
        not (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or carrier_grade_nat
        )
        or address.is_unspecified
    ):
        raise ValueError(f"{name} must target a private, loopback, or Tailscale IP")


def _validate_komga_service_url(name: str, value: str) -> None:
    """Allow one browser/server URL without sending credentials over public HTTP."""

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            f"{name} must be an HTTP(S) service root without credentials, "
            "path, query, or fragment"
        )
    if parsed.scheme == "https":
        return
    _validate_internal_service_url(name, value)


def apply_setting_overrides(settings: Settings, database: Database) -> None:
    """Layer persisted overrides over the environment-derived settings."""

    overrides = database.get_setting_overrides()
    if "release_acquisition_policy" not in overrides:
        legacy = overrides.get("prefer_official_releases")
        if legacy is not None:
            try:
                enabled = str(legacy).strip().casefold() in {"1", "true", "yes", "on"}
                settings.release_acquisition_policy = (
                    "prefer_official" if enabled else "first_available"
                )
            except ValueError:
                pass
    for name, raw in overrides.items():
        if name not in EDITABLE_SETTINGS:
            continue
        try:
            setattr(settings, name, coerce_setting(name, raw))
        except ValueError:
            # A stale invalid override must not prevent startup.
            database.delete_setting(name)


def load_metadata_secret_overrides(settings: Settings) -> None:
    """Load UI-managed integration credentials from the persistent config volume."""

    recover_settings_update(settings)
    values = _read_metadata_secrets(settings)
    for name in MANAGED_SECRET_ENV:
        value = values.get(name)
        if value:
            setattr(settings, name, value)


def settings_view(settings: Settings, database: Database) -> dict[str, Any]:
    overrides = database.get_setting_overrides()
    secret_overrides = _read_metadata_secrets(settings)
    view: dict[str, Any] = {}
    for name, spec in EDITABLE_SETTINGS.items():
        value = (
            settings.komga_effective_auth_method
            if name == "komga_auth_method"
            else settings.effective_release_acquisition_policy
            if name == "release_acquisition_policy"
            else getattr(settings, name)
        )
        if spec.secret:
            shown: Any = SECRET_PLACEHOLDER if value else None
        else:
            shown = value
        view[name] = {
            "value": shown,
            "secret": spec.secret,
            "overridden": name in overrides or name in secret_overrides,
        }
    return view


SPINE_SOURCE_FLAGS: dict[str, str] = {"mangabaka": "metadata_enabled"}


def update_settings(
    settings: Settings, database: Database, changes: dict[str, Any]
) -> list[str]:
    """Validate, persist, and apply changes; returns the touched keys."""

    with SETTINGS_WRITE_LOCK:
        recover_settings_update(settings, database)
        return _update_settings_locked(settings, database, changes)


def _update_settings_locked(
    settings: Settings, database: Database, changes: dict[str, Any]
) -> list[str]:

    coerced: dict[str, Any] = {}
    for name, raw in changes.items():
        if name not in EDITABLE_SETTINGS:
            raise ValueError(f"Unknown setting: {name}")
        spec = EDITABLE_SETTINGS[name]
        if raw is None:
            raw = ""
        text = str(raw)
        if spec.secret and text == SECRET_PLACEHOLDER:
            continue  # An untouched masked field means "keep the current value".
        coerced[name] = coerce_setting(name, text)

    action = coerced.get(
        "torrent_completed_action",
        getattr(settings, "torrent_completed_action", "seed"),
    )
    if action not in {"seed", "remove_after_import", "remove_when_seeded"}:
        raise ValueError(
            "torrent_completed_action must be seed, remove_after_import or remove_when_seeded"
        )
    if {"auth_username", "auth_password"}.intersection(coerced) and (
        generated_login_active(settings)
    ):
        # The login Tankarr created is only a fallback: saving either half of
        # it from Settings stores both, so the saved login stands on its own.
        coerced.setdefault("auth_username", settings.auth_username)
        coerced.setdefault("auth_password", settings.auth_password)
    proposed_username = coerced.get("auth_username", settings.auth_username)
    if coerced.get("translation_enabled", settings.translation_enabled):
        for key in (
            "translation_ai_url",
            "translation_ai_model",
            "translation_ai_api_key",
        ):
            if not coerced.get(key, getattr(settings, key)):
                raise ValueError(
                    f"{key} is required while translation fallback is enabled"
                )
    proposed_password = coerced.get("auth_password", settings.auth_password)
    if bool(proposed_username) != bool(proposed_password):
        raise ValueError("Authentication username and password must be set together")
    proposed_prowlarr_enabled = coerced.get(
        "prowlarr_enabled", settings.prowlarr_enabled
    )
    proposed_prowlarr_api_key = coerced.get(
        "prowlarr_api_key", settings.prowlarr_api_key
    )
    if proposed_prowlarr_enabled and not proposed_prowlarr_api_key:
        raise ValueError("Prowlarr API key is required while Prowlarr is enabled")
    proposed_komga_enabled = coerced.get(
        "komga_link_enabled", settings.komga_link_enabled
    )
    proposed_komga_url = coerced.get("komga_url", settings.komga_url)
    proposed_komga_auth_method = coerced.get(
        "komga_auth_method", settings.komga_auth_method
    )
    proposed_komga_api_key = coerced.get("komga_api_key", settings.komga_api_key)
    proposed_komga_username = coerced.get("komga_username", settings.komga_username)
    proposed_komga_password = coerced.get("komga_password", settings.komga_password)
    if bool(proposed_komga_username) != bool(proposed_komga_password):
        raise ValueError("Komga username and password must be set together")
    if proposed_komga_enabled and not proposed_komga_url:
        raise ValueError("Komga server URL is required while the shortcut is enabled")
    effective_komga_auth_method = proposed_komga_auth_method
    if effective_komga_auth_method == "auto":
        effective_komga_auth_method = (
            "api_key"
            if proposed_komga_api_key
            else "basic"
            if proposed_komga_username and proposed_komga_password
            else "api_key"
        )
    if proposed_komga_enabled:
        if effective_komga_auth_method == "api_key" and not proposed_komga_api_key:
            raise ValueError(
                "Komga API key is required for the selected authentication method"
            )
        if effective_komga_auth_method == "basic" and not (
            proposed_komga_username and proposed_komga_password
        ):
            raise ValueError(
                "Komga username and password are required for the selected "
                "authentication method"
            )
    proposed_default_language = coerced.get(
        "default_language", settings.default_language
    )
    proposed_search_languages = normalize_language_list(
        coerced.get("search_languages", settings.search_languages)
    ).split(",")
    if proposed_default_language not in proposed_search_languages:
        raise ValueError("Default language must be enabled in Search languages")
    if (
        {"auth_username", "auth_password"}.intersection(coerced)
        and not proposed_username
        and settings.auth_required
    ):
        raise ValueError(
            "Authentication cannot be disabled; TANKARR_AUTH_REQUIRED=false "
            "allows it on development machines"
        )
    if (
        {"auth_username", "auth_password"}.intersection(coerced)
        and not proposed_username
        and settings.host not in {"127.0.0.1", "::1", "localhost"}
    ):
        raise ValueError(
            "Authentication cannot be disabled while Tankarr listens outside localhost"
        )
    candidate_values = settings.model_dump()
    candidate_values.update(coerced)
    try:
        candidate = Settings.model_validate(candidate_values)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    # Persist the values after Pydantic has applied cross-field validation and
    # normalization (notably paired client credentials and service roots).
    coerced = {name: getattr(candidate, name) for name in coerced}
    metadata_secrets = _read_metadata_secrets(settings)
    metadata_secrets_changed = False
    database_changes: dict[str, str] = {}
    for name, value in coerced.items():
        if name in MANAGED_SECRET_ENV:
            metadata_secrets_changed = True
            if value:
                metadata_secrets[name] = str(value)
            else:
                metadata_secrets.pop(name, None)
            continue
        database_changes[name] = "" if value is None else str(value)
    delete_keys = (
        ["prefer_official_releases"] if "release_acquisition_policy" in coerced else []
    )
    journal = settings.data_dir / ".settings-transaction.json"
    if metadata_secrets_changed:
        secret_path = settings.metadata_secrets_path
        if secret_path.is_symlink():
            raise OSError("Managed settings secrets must not be a symlink")
        old_contents = secret_path.read_bytes() if secret_path.exists() else None
        token = uuid.uuid4().hex
        _write_private_file(
            journal,
            json.dumps(
                {
                    "version": 1,
                    "token": token,
                    "previous": base64.b64encode(old_contents).decode("ascii")
                    if old_contents is not None
                    else None,
                }
            ).encode(),
        )
        database_changes[SETTINGS_COMMIT_KEY] = token
    try:
        database.apply_setting_changes(
            database_changes,
            delete_keys=delete_keys,
            before_commit=(lambda: _write_metadata_secrets(settings, metadata_secrets))
            if metadata_secrets_changed
            else None,
        )
    except Exception:
        if metadata_secrets_changed:
            recover_settings_update(settings, database)
        raise
    # Only a committed update becomes observable through the live Settings.
    for name, value in coerced.items():
        setattr(settings, name, value)
    if {"auth_username", "auth_password"}.intersection(coerced):
        forget_generated_login(settings)
    if metadata_secrets_changed:
        # A leftover committed journal is harmless and is cleaned on startup.
        try:
            journal.unlink(missing_ok=True)
            _sync_directory(journal.parent)
        except OSError:
            pass
    return sorted(coerced)


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_private_file(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def recover_settings_update(
    settings: Settings, database: Database | None = None
) -> None:
    """Recover a settings/secret-file transaction interrupted around DB commit."""

    with SETTINGS_WRITE_LOCK:
        journal = settings.data_dir / ".settings-transaction.json"
        if not journal.exists():
            return
        if journal.is_symlink() or settings.metadata_secrets_path.is_symlink():
            raise OSError("Refusing symlinked settings transaction files")
        try:
            state = json.loads(journal.read_bytes())
            if state.get("version") != 1 or not isinstance(state.get("token"), str):
                raise ValueError("Invalid settings recovery journal")
        except (ValueError, AttributeError) as exc:
            # A corrupt journal must not keep the application from starting:
            # set it aside for the operator and carry on with what the
            # database and the secret file say now.
            quarantine = journal.with_name(
                f".settings-transaction.corrupt-{int(time.time())}.json"
            )
            journal.replace(quarantine)
            _sync_directory(settings.data_dir)
            logger.warning(
                "Settings recovery journal unreadable (%s); moved to %s",
                exc,
                quarantine.name,
            )
            return
        database = database or Database(settings.database_path)
        if database.get_setting_overrides().get(SETTINGS_COMMIT_KEY) != state["token"]:
            previous = state.get("previous")
            if previous is None:
                settings.metadata_secrets_path.unlink(missing_ok=True)
                _sync_directory(settings.data_dir)
            else:
                _write_private_file(
                    settings.metadata_secrets_path,
                    base64.b64decode(previous, validate=True),
                )
        journal.unlink()
        _sync_directory(journal.parent)


def preview_settings(
    settings: Settings,
    changes: dict[str, Any],
    *,
    allowed: set[str],
) -> Settings:
    """Validate unsaved form values without persisting or mutating settings."""

    values = settings.model_dump()
    for name, raw in changes.items():
        if name not in allowed or name not in EDITABLE_SETTINGS:
            raise ValueError(f"Setting {name!r} cannot be used by this connection test")
        spec = EDITABLE_SETTINGS[name]
        text = "" if raw is None else str(raw)
        if spec.secret and text == SECRET_PLACEHOLDER:
            continue
        values[name] = coerce_setting(name, text)
    try:
        return Settings.model_validate(values)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _read_metadata_secrets(settings: Settings) -> dict[str, str]:
    path = settings.metadata_secrets_path
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return {}
    by_environment = {
        environment: name for name, environment in MANAGED_SECRET_ENV.items()
    }
    result: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        environment, raw = stripped.split("=", 1)
        name = by_environment.get(environment.strip())
        if name is None:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(value, str) and value:
            result[name] = value
    return result


def _write_metadata_secrets(settings: Settings, values: dict[str, str]) -> None:
    path = settings.metadata_secrets_path
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Managed by Tankarr Settings. Do not commit this file.",
        *(
            f"{MANAGED_SECRET_ENV[name]}={json.dumps(values[name])}"
            for name in MANAGED_SECRET_ENV
            if values.get(name)
        ),
        "",
    ]
    _write_private_file(path, "\n".join(lines).encode("utf-8"))
