"""Provider adapters and their shared registry."""

from __future__ import annotations

from tankarr.providers.base import Provider
from tankarr.providers.local import LocalProvider
from tankarr.providers.suwayomi import SuwayomiProvider

CORE_PROVIDER_NAMES = frozenset({"mangadex", "local"})
CONFIGURABLE_PROVIDER_NAMES = frozenset({"suwayomi"})


def build_providers(settings) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if settings.suwayomi_enabled:
        username, password = settings.suwayomi_effective_credentials
        suwayomi = SuwayomiProvider(
            settings.suwayomi_effective_url,
            username=username,
            password=password,
            language=settings.suwayomi_language,
            source_ids=settings.suwayomi_source_id_set,
            instance_token=settings.suwayomi_instance_token,
            search_timeout_seconds=settings.suwayomi_search_timeout_seconds,
            timeout_seconds=settings.request_timeout_seconds,
        )
        providers[suwayomi.name] = suwayomi
    local = LocalProvider(settings.data_dir / "covers")
    providers[local.name] = local
    return providers


def replace_configurable_providers(
    providers: dict[str, Provider], settings
) -> tuple[list[Provider], list[Provider]]:
    """Hot-swap optional providers while preserving the shared registry object.

    Services keep a reference to ``providers``, so mutating it in place makes
    Settings changes visible immediately. Existing optional instances are
    returned as retired rather than closed: an in-flight download may still be
    using one. Freshly built core instances are safe to close immediately.
    """

    rebuilt = build_providers(settings)
    discarded: list[Provider] = []
    for name in CORE_PROVIDER_NAMES:
        current = providers.get(name)
        replacement = rebuilt.get(name)
        if current is not None and replacement is not None:
            rebuilt[name] = current
            discarded.append(replacement)

    retired = [
        item for name, item in providers.items() if name in CONFIGURABLE_PROVIDER_NAMES
    ]
    providers.clear()
    providers.update(rebuilt)
    return retired, discarded
