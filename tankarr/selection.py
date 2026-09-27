"""Explain actual acquisition decisions without changing their outcome."""

from datetime import UTC, datetime
from typing import Any

from tankarr.database import Database
from tankarr.source_circuit import source_gate_key
from tankarr.source_ranking import group_phase, source_class


def explain_releases(
    database: Database, manga_id: str, releases: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rejections: dict[str, list[str]] = {}
    selected = {
        str(row["id"])
        for row in database.preferred_missing_releases(manga_id, explain=rejections)
        if not row.get("blocked")
    }
    phase = group_phase(releases)
    now = datetime.now(UTC).timestamp()
    result = []
    for release in releases:
        key = str(release["id"])
        retry_at = database.source_retry_at(
            source_gate_key(
                release.get("provider"),
                release.get("source_url"),
                release.get("source_name"),
            )
        )
        reasons = list(rejections.get(key, []))
        if retry_at > now:
            reasons.append("Source temporarily unavailable; automatic retry scheduled")
        chosen = key in selected
        if not chosen and not reasons:
            reasons.append(
                "Another release is preferred for this slot, or its content is already owned"
            )
        detail = source_class(
            provider=release.get("provider"),
            source_key=release.get("source_key"),
            source_name=release.get("source_name"),
        )
        result.append(
            {
                **release,
                "selection": {
                    "selected": chosen,
                    "reasons": (
                        [
                            f"Preferred eligible release for this slot ({phase}; {detail})"
                        ]
                        if chosen
                        else []
                    )
                    + reasons,
                    "source_class": detail,
                    "source_priority": database.source_ranking.explicit_rank(
                        release, phase
                    ),
                    "next_retry_at": retry_at if retry_at > now else None,
                },
            }
        )
    return result
