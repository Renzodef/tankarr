"""Prometheus metrics at ``/metrics``, in the text exposition format, no dependency.

The numbers come from a handful of SQL aggregates and the state the background
loops already keep, so a scrape every fifteen seconds costs a few milliseconds
and never renders a page. The endpoint sits behind the normal authentication:
give Prometheus the API key as a bearer token (``authorization: credentials:``
in the scrape configuration) or the login as basic auth.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from tankarr import __version__

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _timestamp(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return parsed.timestamp()


class Exposition:
    """Collects samples and renders them with HELP and TYPE lines."""

    def __init__(self) -> None:
        self._lines: list[str] = []

    def gauge(
        self,
        name: str,
        help_text: str,
        samples: Iterable[tuple[dict[str, object], float | int]],
    ) -> None:
        rows = list(samples)
        self._lines.append(f"# HELP {name} {help_text}")
        self._lines.append(f"# TYPE {name} gauge")
        for labels, value in rows:
            rendered = ",".join(
                f'{key}="{_escape(item)}"' for key, item in sorted(labels.items())
            )
            label_text = f"{{{rendered}}}" if rendered else ""
            number = int(value) if float(value).is_integer() else float(value)
            self._lines.append(f"{name}{label_text} {number}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def database_samples(connection: sqlite3.Connection) -> dict[str, Any]:
    """The aggregates a scrape needs, from one connection."""

    series = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(monitored), 0) FROM manga"
    ).fetchone()
    releases = connection.execute(
        "SELECT COUNT(*), COALESCE(SUM(downloaded), 0) FROM chapter_release"
    ).fetchone()
    jobs = connection.execute(
        "SELECT status, COUNT(*) FROM download_job GROUP BY status"
    ).fetchall()
    torrents = connection.execute(
        "SELECT status, COUNT(*) FROM torrent_download GROUP BY status"
    ).fetchall()
    return {
        "series_total": int(series[0]),
        "series_monitored": int(series[1]),
        "releases_total": int(releases[0]),
        "releases_downloaded": int(releases[1]),
        "jobs": {str(row[0]): int(row[1]) for row in jobs},
        "torrents": {str(row[0]): int(row[1]) for row in torrents},
    }


def render_metrics(
    *,
    counts: dict[str, Any],
    tasks: list[dict[str, Any]],
    volumes: dict[str, Path],
    started_at: object,
    update: dict[str, Any],
    now: float,
) -> str:
    exposition = Exposition()
    exposition.gauge(
        "tankarr_info", "Tankarr version.", [({"version": __version__}, 1)]
    )
    exposition.gauge(
        "tankarr_uptime_seconds",
        "Seconds since the application started.",
        [({}, max(0.0, now - _timestamp(started_at)) if started_at else 0.0)],
    )
    exposition.gauge(
        "tankarr_series_total", "Series in the library.", [({}, counts["series_total"])]
    )
    exposition.gauge(
        "tankarr_series_monitored",
        "Series Tankarr monitors for new releases.",
        [({}, counts["series_monitored"])],
    )
    exposition.gauge(
        "tankarr_releases_total",
        "Releases known from every source.",
        [({}, counts["releases_total"])],
    )
    exposition.gauge(
        "tankarr_releases_downloaded",
        "Releases that own a file in the library.",
        [({}, counts["releases_downloaded"])],
    )
    exposition.gauge(
        "tankarr_download_jobs",
        "Download jobs by status.",
        [
            ({"status": status}, count)
            for status, count in sorted(counts["jobs"].items())
        ],
    )
    exposition.gauge(
        "tankarr_torrent_downloads",
        "Downloads handed to qBittorrent, SABnzbd or archive.org, by status.",
        [
            ({"status": status}, count)
            for status, count in sorted(counts["torrents"].items())
        ],
    )
    free: list[tuple[dict[str, object], float]] = []
    total: list[tuple[dict[str, object], float]] = []
    for name, path in volumes.items():
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            continue
        free.append(({"volume": name}, usage.free))
        total.append(({"volume": name}, usage.total))
    exposition.gauge(
        "tankarr_disk_free_bytes", "Free space of the data and library volumes.", free
    )
    exposition.gauge(
        "tankarr_disk_total_bytes", "Size of the data and library volumes.", total
    )
    exposition.gauge(
        "tankarr_task_last_run_timestamp_seconds",
        "When each scheduled task last ran (0 when it never did).",
        [({"task": task["id"]}, _timestamp(task.get("last_run_at"))) for task in tasks],
    )
    exposition.gauge(
        "tankarr_task_running",
        "Whether each scheduled task is running now.",
        [({"task": task["id"]}, 1 if task.get("running") else 0) for task in tasks],
    )
    exposition.gauge(
        "tankarr_task_failed",
        "Whether each scheduled task's last run reported an error.",
        [({"task": task["id"]}, 1 if task.get("last_error") else 0) for task in tasks],
    )
    exposition.gauge(
        "tankarr_update_available",
        "Whether a newer Tankarr release exists.",
        [({}, 1 if update.get("update_available") else 0)],
    )
    return exposition.render()
