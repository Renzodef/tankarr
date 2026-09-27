"""Batched, incremental inputs for Library cards.

Callers hold Database.read_snapshot() while comparing revisions and reading
changed cards. The number of queries is independent of the library size.
"""

import json
from typing import TYPE_CHECKING, Any

from tankarr.chapter_map import MapEntry, resolved_map

if TYPE_CHECKING:
    from tankarr.database import Database


# Every table consulted by a Library card, including publication signals and
# the recovery evidence that decides whether to follow books or chapters.
LIBRARY_TABLES = (
    ("chapter_release", "updated_at"),
    ("series_metadata", "last_enriched_at"),
    ("series_chapter_map", "updated_at"),
    ("volume_monitor_override", "updated_at"),
    ("indexer_offer", "seen_at"),
    ("manga_author", "updated_at"),
    ("metadata_source_record", "fetched_at"),
    ("manga_release_source", "updated_at"),
    ("wanted_attempt", "attempted_at"),
)

LIBRARY_REVISION_TABLES = ("manga", *(table for table, _ in LIBRARY_TABLES), "author")
CALENDAR_REVISION_TABLES = (
    "manga",
    "chapter_release",
    "series_metadata",
    "metadata_source_record",
    "manga_release_source",
)
WANTED_REVISION_TABLES = (
    *LIBRARY_REVISION_TABLES,
    "release_block",
    "download_job",
    "source_health",
    "source_failure",
)


def install_revision_triggers(connection) -> None:
    """Small transactional clocks avoid scanning every release on a warm GET.

    SQLite triggers also observe writes from other Database objects/processes;
    an in-memory counter or PRAGMA data_version on short-lived connections does
    not. Clocks roll back with the mutation and require no catalogue backfill.
    """

    connection.execute(
        "CREATE TABLE IF NOT EXISTS read_model_revision ("
        "table_name TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS read_model_series_revision ("
        "manga_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 0)"
    )
    active = "('queued','running','downloading','packaging','importing')"
    for table in dict.fromkeys((*WANTED_REVISION_TABLES, *CALENDAR_REVISION_TABLES)):
        connection.execute(
            "INSERT OR IGNORE INTO read_model_revision(table_name) VALUES (?)", (table,)
        )
        for operation in ("INSERT", "UPDATE", "DELETE"):
            condition = ""
            series_clock = ""
            if table in {name for name, _timestamp in LIBRARY_TABLES} | {"manga"}:
                field = "id" if table == "manga" else "manga_id"
                aliases = ("OLD",) if operation == "DELETE" else ("NEW",)
                for alias in aliases:
                    series_clock += (
                        "INSERT INTO read_model_series_revision(manga_id,revision) "
                        f"VALUES ({alias}.{field}, 1) ON CONFLICT(manga_id) DO UPDATE "
                        "SET revision=revision+1; "
                    )
                if operation == "UPDATE":
                    series_clock += (
                        "INSERT INTO read_model_series_revision(manga_id,revision) "
                        f"SELECT OLD.{field}, 1 WHERE OLD.{field} IS NOT NEW.{field} "
                        "ON CONFLICT(manga_id) DO UPDATE SET revision=revision+1; "
                    )
                if table == "manga" and operation == "DELETE":
                    series_clock += (
                        "DELETE FROM read_model_series_revision WHERE manga_id=OLD.id; "
                    )
            if table == "download_job":
                if operation == "INSERT":
                    condition = f"WHEN NEW.status IN {active}"
                elif operation == "DELETE":
                    condition = f"WHEN OLD.status IN {active}"
                else:
                    condition = (
                        f"WHEN (OLD.status IN {active} OR NEW.status IN {active}) "
                        "AND (OLD.status IS NOT NEW.status "
                        "OR OLD.chapter_id IS NOT NEW.chapter_id "
                        "OR OLD.manga_id IS NOT NEW.manga_id)"
                    )
            trigger_name = f"read_model_{table}_{operation.lower()}"
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
            connection.execute(
                f"CREATE TRIGGER {trigger_name} "
                f"AFTER {operation} ON {table} {condition} BEGIN "
                f"UPDATE read_model_revision SET revision=revision+1 WHERE table_name='{table}'; "
                f"{series_clock} END"
            )


# Coverage does not use the large numbering-evidence JSON, hashes or import
# paths on every release. Wanted hydrates only its surviving candidates.
CHAPTER_SUMMARY_COLUMNS = """
    c.id, c.manga_id, c.volume,
    CASE WHEN COALESCE(c.release_unit, 'chapter') <> 'volume'
              AND COALESCE(c.numbering_status, 'mapped') = 'mapped'
         THEN c.canonical_chapter ELSE NULL END AS chapter,
    c.title, c.language, c.provider, c.source_name, c.source_key,
    c.release_unit, c.publish_at, c.source_url, c.pages, c.version,
    c.downloaded, c.monitored, c.assembled_from, c.library_sha256
"""


def decode_chapter_summary(row) -> dict[str, Any]:
    result = dict(row)
    result["downloaded"] = bool(result["downloaded"])
    result["monitored"] = bool(result["monitored"])
    return result


def manga_revisions(database: "Database") -> dict[str, tuple[str, ...]]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT m.id, COALESCE(r.revision,0), "
            "COALESCE((SELECT revision FROM read_model_revision WHERE table_name='author'),0) "
            "FROM manga m LEFT JOIN read_model_series_revision r ON r.manga_id=m.id "
            "ORDER BY lower(COALESCE(NULLIF(trim(m.title_override), ''), "
            "NULLIF(trim(m.metadata_title), ''), m.title))"
        ).fetchall()
    return {str(row[0]): tuple(str(value or "") for value in row[1:]) for row in rows}


def changed_inputs(
    database: "Database", manga_ids: list[str]
) -> dict[str, dict[str, Any]]:
    if not manga_ids:
        return {}
    placeholders = ",".join("?" for _ in manga_ids)
    inputs = {
        str(manga["id"]): {
            "manga": manga,
            "metadata": None,
            "authors": [],
            "overrides": {},
            "chapter_map": [],
            "releases": [],
            "publication_signals": {},
            "indexer_volumes": set(),
            "unobtainable_volumes": set(),
            "pending_volumes": set(),
        }
        for manga in database.list_manga(manga_ids)
    }
    for manga_id, volumes in database.pending_book_volumes(manga_ids).items():
        if manga_id in inputs:
            inputs[manga_id]["pending_volumes"] = set(volumes)
    with database.connect() as connection:
        for row in connection.execute(
            f"SELECT * FROM series_metadata WHERE manga_id IN ({placeholders})",
            manga_ids,
        ):
            inputs[str(row["manga_id"])]["metadata"] = (
                database._decode_series_metadata_row(row)
            )
        for row in connection.execute(
            f"SELECT * FROM volume_monitor_override WHERE manga_id IN ({placeholders})",
            manga_ids,
        ):
            inputs[str(row["manga_id"])]["overrides"][str(row["volume_key"])] = str(
                row["state"]
            )
        for row in connection.execute(
            "SELECT * FROM series_chapter_map "
            f"WHERE manga_id IN ({placeholders}) ORDER BY exact DESC, volumes, chapters",
            manga_ids,
        ):
            inputs[str(row["manga_id"])]["chapter_map"].append(
                MapEntry(
                    volumes=tuple(str(row["volumes"]).split(",")),
                    chapters=tuple(str(row["chapters"]).split(",")),
                    exact=bool(row["exact"]),
                    source=str(row["source"]),
                    release_date=row["release_date"],
                )
            )
        for row in connection.execute(
            f"SELECT {CHAPTER_SUMMARY_COLUMNS} FROM chapter_release c "
            "JOIN manga m ON m.id=c.manga_id "
            f"WHERE c.manga_id IN ({placeholders}) AND c.language=m.preferred_language "
            "ORDER BY CASE WHEN CAST(c.volume AS REAL) IS NULL THEN 1 ELSE 0 END, "
            "CAST(c.volume AS REAL), "
            "CASE WHEN CAST(c.chapter AS REAL) IS NULL THEN 1 ELSE 0 END, "
            "CAST(c.chapter AS REAL), c.publish_at, c.id",
            manga_ids,
        ):
            inputs[str(row["manga_id"])]["releases"].append(decode_chapter_summary(row))
        for row in connection.execute(
            "SELECT ma.manga_id, a.id, a.display_name, "
            "ma.credited_names_json, ma.roles_json "
            "FROM manga_author ma JOIN author a ON a.id=ma.author_id "
            f"WHERE ma.manga_id IN ({placeholders}) AND a.merged_into IS NULL "
            "ORDER BY lower(a.display_name), a.id",
            manga_ids,
        ):
            inputs[str(row["manga_id"])]["authors"].append(
                {
                    "id": str(row["id"]),
                    "name": str(row["display_name"]),
                    "credited_names": json.loads(row["credited_names_json"] or "[]"),
                    "roles": json.loads(row["roles_json"] or "[]"),
                }
            )
        for row in connection.execute(
            "SELECT manga_id, source, json_extract(data_json, '$.status') AS status "
            f"FROM metadata_source_record WHERE manga_id IN ({placeholders}) "
            "AND source IN ('mangabaka', 'myanimelist', 'mangaupdates') "
            "ORDER BY fetched_at",
            manga_ids,
        ):
            status = str(row["status"] or "").strip().casefold()
            if status:
                inputs[str(row["manga_id"])]["publication_signals"][row["source"]] = (
                    status
                )
        for row in connection.execute(
            "SELECT manga_id, publication_status FROM manga_release_source "
            f"WHERE manga_id IN ({placeholders}) AND enabled=1 "
            "AND source_role IN ('primary_official', 'secondary_official') "
            "AND publication_status IS NOT NULL AND publication_status<>'' "
            "ORDER BY CASE source_role WHEN 'primary_official' THEN 0 ELSE 1 END, "
            "updated_at DESC",
            manga_ids,
        ):
            inputs[str(row["manga_id"])]["publication_signals"].setdefault(
                "official", str(row["publication_status"])
            )
        for row in connection.execute(
            f"SELECT manga_id, volume FROM indexer_offer WHERE manga_id IN ({placeholders})",
            manga_ids,
        ):
            try:
                volume = int(float(row["volume"]))
            except (ValueError, TypeError):
                continue
            inputs[str(row["manga_id"])]["indexer_volumes"].add(volume)
        for row in connection.execute(
            "SELECT manga_id, slot_key FROM wanted_attempt "
            f"WHERE manga_id IN ({placeholders}) AND channel='indexer_book' "
            "AND outcome IN ('not_offered', 'ambiguous', 'error') "
            "AND slot_key LIKE 'volume:%'",
            manga_ids,
        ):
            try:
                volume = int(float(str(row["slot_key"]).split(":", 1)[1]))
            except ValueError:
                continue
            inputs[str(row["manga_id"])]["unobtainable_volumes"].add(volume)
    for entry in inputs.values():
        # The stored rows say what each source claims; a card must read them
        # the way the series page does, or a catalogue's partial log passes
        # for a measured map and the grid invents books the page never shows.
        entry["chapter_map"] = resolved_map(entry["chapter_map"])
    return inputs
