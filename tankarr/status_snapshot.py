"""Refresh only System statistics whose stored inputs actually changed."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tankarr.database import Database


# None observes every update; an empty tuple observes only inserts/deletes.
# Timestamps, download progress and metadata payloads do not change counts.
STATUS_INPUTS = {
    "manga": ("manga_totals", {"manga": ("monitored",)}),
    "chapters": (
        "chapter_totals",
        {
            "manga": ("id", "preferred_language"),
            "chapter_release": (
                "id",
                "manga_id",
                "language",
                "volume",
                "chapter",
                "downloaded",
            ),
        },
    ),
    "jobs": ("job_counts", {"download_job": ("status",)}),
    "coverage": (
        "metadata_overview",
        {
            "manga": (),
            "series_metadata": ("artwork_path", "last_synced_at", "last_error"),
            "metadata_source_record": (
                "manga_id",
                "entity_key",
                "entity_type",
                "source",
            ),
            "metadata_provider_correlation": ("manga_id",),
            "metadata_manual_correlation": ("manga_id",),
            "volume_metadata": ("manga_id", "volume_key", "artwork_path"),
            "chapter_release": ("manga_id", "volume", "release_unit", "chapter"),
            "metadata_sync_state": ("last_error",),
        },
    ),
    "alert_rows": (
        "list_status_alert_rows",
        {
            "manga": (
                "id",
                "title",
                "title_override",
                "metadata_title",
                "preferred_language",
                "provider",
                "primary_source_error",
                "primary_source_error_since",
            )
        },
    ),
    "release_sources": ("list_all_release_sources", {"manga_release_source": None}),
}


def install_status_revision_triggers(connection) -> None:
    """Clocks follow committed writes, including other processes and deletes."""

    for section, (_method, tables) in STATUS_INPUTS.items():
        key = f"status:{section}"
        connection.execute(
            "INSERT OR IGNORE INTO read_model_revision(table_name) VALUES (?)", (key,)
        )
        for table, columns in tables.items():
            for operation in ("INSERT", "UPDATE", "DELETE"):
                name = f"system_status_{section}_{table}_{operation.lower()}"
                connection.execute(f"DROP TRIGGER IF EXISTS {name}")
                if operation == "UPDATE" and columns == ():
                    continue
                condition = ""
                if operation == "UPDATE" and columns is not None:
                    condition = "WHEN " + " OR ".join(
                        f"OLD.{column} IS NOT NEW.{column}" for column in columns
                    )
                connection.execute(
                    f"CREATE TRIGGER {name} AFTER {operation} ON {table} "
                    f"{condition} BEGIN UPDATE read_model_revision "
                    f"SET revision=revision+1 WHERE table_name='{key}'; END"
                )


def read_status_updates(database: "Database", previous: dict[str, int]):
    """Read clocks and changed sections from one consistent SQLite snapshot.

    The caller publishes updates and their clocks only after all reads succeed.
    An unchanged library costs six small clock rows, not a catalogue scan.
    """

    keys = tuple(f"status:{section}" for section in STATUS_INPUTS)
    with database.read_snapshot():
        with database.connect() as connection:
            rows = connection.execute(
                "SELECT table_name, revision FROM read_model_revision "
                f"WHERE table_name IN ({','.join('?' for _ in keys)})",
                keys,
            ).fetchall()
        revisions = {str(row[0]).removeprefix("status:"): int(row[1]) for row in rows}
        updates = {
            section: getattr(database, method)()
            for section, (method, _tables) in STATUS_INPUTS.items()
            if section not in previous or previous[section] != revisions[section]
        }
    return revisions, updates
