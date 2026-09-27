"""Direct chapter-download providers and their operator-ordered priority."""

from __future__ import annotations

DOWNLOAD_PROVIDER_PRIORITY_DEFAULT: tuple[str, ...] = ("suwayomi",)
DOWNLOAD_PROVIDER_NAMES = frozenset(DOWNLOAD_PROVIDER_PRIORITY_DEFAULT)


def normalize_provider_priority(value: object) -> str:
    """Return a canonical, total priority order as a comma-separated string.

    Unknown names are rejected. Omitted providers keep their default relative
    order after the explicit ones, so the order stays total even when the
    operator lists only the providers they care about.
    """

    selected: list[str] = []
    for raw in str(value or "").split(","):
        name = raw.strip().casefold()
        if not name:
            continue
        if name not in DOWNLOAD_PROVIDER_NAMES:
            known = ", ".join(DOWNLOAD_PROVIDER_PRIORITY_DEFAULT)
            raise ValueError(
                f"Unknown download provider in priority list: {name!r} "
                f"(known providers: {known})"
            )
        if name not in selected:
            selected.append(name)
    selected.extend(
        name for name in DOWNLOAD_PROVIDER_PRIORITY_DEFAULT if name not in selected
    )
    return ",".join(selected)


def provider_priority_order(value: object) -> tuple[str, ...]:
    """Parse one priority string into the total provider order it defines."""

    return tuple(normalize_provider_priority(value).split(","))
