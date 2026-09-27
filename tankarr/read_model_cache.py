"""Time dependencies shared by the HTTP and canonical-series caches."""

import hashlib
from collections import OrderedDict
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from tankarr.source_ranking import FRESH_WINDOW_DAYS

# Publication dates, ranking windows and decaying source health change even
# when SQLite has not been written to. HTTP revalidation observes those changes
# within this interval; unchanged Library cards retain their individual cache.
CACHE_CLOCK_SECONDS = 30


class SnapshotValidators:
    """Bounded identity cache: unchanged immutable bytes are hashed only once.

    Retaining the bytes also prevents Python from reusing their id while an
    entry is live. The validator always describes the bytes served, including
    stale-while-revalidate responses, never the latest database revision.
    """

    def __init__(self, limit: int = 4) -> None:
        self.limit = max(1, limit)
        self.entries: OrderedDict[tuple[str, int], tuple[bytes, str]] = OrderedDict()

    def etag(self, kind: str, payload: bytes) -> str:
        key = kind, id(payload)
        if key not in self.entries:
            tag = f'"{kind}-{hashlib.sha256(payload).hexdigest()[:24]}"'
            self.entries[key] = payload, tag
            while len(self.entries) > self.limit:
                self.entries.popitem(last=False)
        self.entries.move_to_end(key)
        return self.entries[key][1]


def cache_now() -> float:
    return datetime.now(UTC).timestamp()


def cache_time_revision() -> str:
    return str(int(cache_now() // CACHE_CLOCK_SECONDS))


def next_publication(
    releases: Iterable[dict[str, Any]],
    *,
    since: float | None = None,
    include_ranking: bool = False,
) -> float:
    """When a cached card can next change solely because time passes."""

    now = cache_now() if since is None else since
    upcoming = float("inf")
    for release in releases:
        if release.get("downloaded"):
            continue
        try:
            published = datetime.fromisoformat(
                str(release.get("publish_at") or "").strip().replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        timestamp = published.timestamp()
        if now < timestamp < upcoming:
            upcoming = timestamp
        if include_ranking:
            # group_phase treats the exact window boundary as fresh.
            backfill_at = timestamp + FRESH_WINDOW_DAYS * 86400 + 0.000001
            if now < backfill_at < upcoming:
                upcoming = backfill_at
    return upcoming
