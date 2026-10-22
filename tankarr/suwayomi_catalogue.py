"""Keep a managed Suwayomi database limited to what Tankarr still needs.

Suwayomi persists every search hit and every chapter fetch, and the pinned
server has no API that deletes a manga. Listing the whole catalogue through
GraphQL loads every row into a 256 MB heap and crashes the process, so
cleanup never asks GraphQL for the list. It stops the managed server, then
deletes unneeded rows in small SQL batches with a separate, smaller JVM.
Chapters, pages and metadata follow the manga row through foreign keys.

An external Suwayomi server is not rewritten: its database is not ours, and
it has the same lack of a delete API.
"""

from __future__ import annotations

import csv
import inspect
import io
import logging
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BATCH_ROWS = 10
STATEMENTS_PER_ROUND = 40
ROUND_TIMEOUT_SECONDS = 180
COMPACT_TIMEOUT_SECONDS = 600
LARGE_DATABASE_BYTES = 64 * 1024 * 1024
COMPACT_GROWTH_BYTES = 32 * 1024 * 1024
LARGE_PASS_BUDGET_SECONDS = 20 * 60
SMALL_PASS_BUDGET_SECONDS = 8 * 60
COMPACT_SETTING = "__suwayomi_catalogue_compact_bytes"

CLEANUP_JVM = (
    "-Xmx192m",
    "-XX:MaxDirectMemorySize=32m",
    "-XX:+UseSerialGC",
    "-Xss256k",
    "-XX:ReservedCodeCacheSize=32m",
    "-XX:CompressedClassSpaceSize=32m",
    "-XX:TieredStopAtLevel=1",
    "-XX:CICompilerCount=1",
    "-XX:+ExitOnOutOfMemoryError",
    "-Djava.awt.headless=true",
)

SCHEMA_QUERY = (
    "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
    "WHERE TABLE_SCHEMA = 'PUBLIC'"
)
MANGA_REQUIRED = frozenset(
    {"id", "in_library", "last_fetched_at", "chapters_last_fetched_at"}
)
CHAPTER_FLAGS = ("is_downloaded", "read", "bookmark", "last_read_at")


class CatalogueError(RuntimeError):
    """Suwayomi's database could not be cleaned."""


class CatalogueSchemaError(CatalogueError):
    """The database shape is not the one cleanup knows how to edit."""


class CatalogueHeapError(CatalogueError):
    """The cleanup JVM ran out of memory on one batch."""


class CatalogueCommandError(CatalogueError):
    """H2 rejected a cleanup script."""


def quote_ident(name: str) -> str:
    """Quote one H2 identifier. Anything else is refused, never interpolated."""

    if not IDENT.fullmatch(name):
        raise CatalogueSchemaError(f"refusing SQL identifier {name!r}")
    return f'"{name}"'


def database_file(home: Path) -> Path:
    """The H2 file Suwayomi opens from ``jdbc:h2:${dataRoot}/database``."""

    return home / ".local" / "share" / "Tachidesk" / "database.mv.db"


def jdbc_base(path: Path) -> Path:
    """H2's file URL is the database name without ``.mv.db``."""

    if path.name != "database.mv.db":
        raise CatalogueSchemaError(f"unexpected Suwayomi database name: {path.name}")
    return path.with_name("database")


def csvwrite(path: Path, query: str) -> str:
    """One ``CSVWRITE`` call. The query selects ids or a count, never ``*``."""

    if "SELECT *" in query.upper():
        raise CatalogueSchemaError("cleanup queries must not select every column")
    escaped_path = str(path).replace("'", "''")
    escaped_query = query.replace("'", "''")
    return (
        "CALL CSVWRITE("
        f"'{escaped_path}', '{escaped_query}', "
        "'charset=UTF-8 fieldSeparator=,');"
    )


def parse_count(text: str) -> int:
    rows = list(csv.reader(io.StringIO(text)))
    if len(rows) < 2 or not rows[1] or not rows[1][0].strip():
        raise CatalogueSchemaError("H2 did not write a row count")
    return int(rows[1][0])


def parse_columns(text: str) -> dict[str, dict[str, str]]:
    """``{table: {column.casefold(): actual column name}}``."""

    tables: dict[str, dict[str, str]] = {}
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        if len(row) < 2:
            continue
        table, column = row[0], row[1]
        if not IDENT.fullmatch(table) or not IDENT.fullmatch(column):
            raise CatalogueSchemaError(f"refusing SQL identifier {table!r}.{column!r}")
        tables.setdefault(table, {})[column.casefold()] = column
    return tables


def resolve_table(
    columns: dict[str, dict[str, str]],
    required: frozenset[str],
    preferred: str,
) -> str | None:
    matches = [name for name, fields in columns.items() if required <= set(fields)]
    if not matches:
        return None
    preferred_matches = [name for name in matches if name.casefold() == preferred]
    if len(preferred_matches) == 1:
        return preferred_matches[0]
    if len(matches) == 1:
        return matches[0]
    raise CatalogueSchemaError(f"ambiguous {preferred} table: {matches}")


def _col(fields: dict[str, str], name: str) -> str:
    try:
        actual = fields[name]
    except KeyError as exc:
        raise CatalogueSchemaError(f"missing column {name}") from exc
    return quote_ident(actual)


def keep_id_statements(ids: Iterable[int]) -> list[str]:
    values = []
    for raw in ids:
        value = int(raw)
        if value > 0:
            values.append(str(value))
    statements: list[str] = []
    for start in range(0, len(values), 200):
        chunk = ",".join(f"({item})" for item in values[start : start + 200])
        statements.append(
            "INSERT INTO TANKARR_KEEP (ID) "
            f"SELECT V FROM (VALUES {chunk}) AS T(V) "
            "WHERE NOT EXISTS (SELECT 1 FROM TANKARR_KEEP K WHERE K.ID = V);"
        )
    return statements


def timestamp_keep_sql(manga_table: str, fields: dict[str, str], cutoff: int) -> str:
    table = quote_ident(manga_table)
    manga_id = _col(fields, "id")
    terms = [f"m.{_col(fields, 'in_library')} = TRUE"]
    for name in ("last_fetched_at", "chapters_last_fetched_at", "last_modified_at"):
        if name not in fields:
            continue
        column = _col(fields, name)
        # A zero means Suwayomi never fetched the row. Keeping ``>= 0``
        # would retain every search stub.
        terms.append(f"(m.{column} > 0 AND m.{column} >= {int(cutoff)})")
    joined = " OR ".join(terms)
    return (
        "INSERT INTO TANKARR_KEEP (ID) "
        f"SELECT m.{manga_id} FROM {table} AS m "
        f"WHERE ({joined}) "
        f"AND NOT EXISTS (SELECT 1 FROM TANKARR_KEEP K WHERE K.ID = m.{manga_id});"
    )


def chapter_keep_sql(chapter_table: str, fields: dict[str, str]) -> str:
    flags = []
    for name in CHAPTER_FLAGS:
        if name not in fields:
            continue
        column = _col(fields, name)
        if name == "last_read_at":
            flags.append(f"c.{column} > 0")
        else:
            flags.append(f"c.{column} = TRUE")
    if not flags:
        raise CatalogueSchemaError(
            "chapter table has no download, read or bookmark column"
        )
    table = quote_ident(chapter_table)
    manga_id = _col(fields, "manga")
    return (
        "INSERT INTO TANKARR_KEEP (ID) "
        f"SELECT DISTINCT c.{manga_id} FROM {table} AS c "
        f"WHERE ({' OR '.join(flags)}) "
        f"AND NOT EXISTS (SELECT 1 FROM TANKARR_KEEP K WHERE K.ID = c.{manga_id});"
    )


def delete_sql(manga_table: str, fields: dict[str, str], batch: int) -> str:
    if batch < 1:
        raise CatalogueSchemaError("delete batch must be at least 1")
    return (
        f"DELETE FROM {quote_ident(manga_table)} "
        f"WHERE {_col(fields, 'id')} NOT IN (SELECT ID FROM TANKARR_KEEP) "
        f"LIMIT {int(batch)};"
    )


def count_query(manga_table: str, fields: dict[str, str]) -> str:
    return (
        f"SELECT COUNT(*) AS N FROM {quote_ident(manga_table)} "
        f"WHERE {_col(fields, 'id')} NOT IN (SELECT ID FROM TANKARR_KEEP)"
    )


def catalogue_pass_is_recent(
    stored: dict[str, Any] | None,
    *,
    now: datetime,
    interval_seconds: float,
) -> bool:
    """True when the last finished cleanup is still inside its interval.

    A deferred, failed or aborted pass is not recent, so the next caller
    retries. Callers that skip for this reason must not rewrite the stored
    timestamp: that would push a real pass further away without doing one.
    """

    if not isinstance(stored, dict):
        return False
    result = stored.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("error") or result.get("deferred") or result.get("aborted"):
        return False
    if "deleted" not in result:
        return False
    raw = stored.get("at")
    if not raw:
        return False
    text = str(raw).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return (now - when).total_seconds() < float(interval_seconds)


def compact_due(size: int, previous: int | None) -> bool:
    """Compact a large file once, then only after it has grown again.

    Rewriting a stable database every day wears a small disk and does not
    free space H2 has already returned.
    """

    if size <= LARGE_DATABASE_BYTES:
        return False
    if previous is None:
        return True
    return size >= previous + COMPACT_GROWTH_BYTES


def _csv_path(statement: str) -> Path | None:
    marker = "CSVWRITE('"
    start = statement.find(marker)
    if start < 0:
        return None
    start += len(marker)
    end = statement.find("'", start)
    if end < 0:
        return None
    return Path(statement[start:end].replace("''", "'"))


def execute_h2(
    java: str,
    jar: Path,
    base: Path,
    statements: Sequence[str],
    timeout: float,
) -> str:
    """Run one H2 script. ``IFEXISTS`` refuses to create a new database."""

    script = "\n".join(statements)
    if "CACHE_SIZE" in script.upper():
        raise CatalogueSchemaError("cleanup must not persist an H2 cache size")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sql", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        escaped = str(script_path).replace("'", "''")
        command = [
            java,
            *CLEANUP_JVM,
            "-cp",
            str(jar),
            "org.h2.tools.Shell",
            "-url",
            f"jdbc:h2:{base};IFEXISTS=TRUE",
            "-user",
            "sa",
            "-password",
            "",
            "-sql",
            f"RUNSCRIPT FROM '{escaped}'",
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise CatalogueCommandError(
                f"H2 cleanup script exceeded {int(timeout)}s"
            ) from exc
        output = f"{completed.stdout or ''}\n{completed.stderr or ''}"
        if completed.returncode != 0:
            if "ClassNotFoundException" in output and "org.h2.tools.Shell" in output:
                raise CatalogueCommandError(
                    "Suwayomi's server JAR does not expose org.h2.tools.Shell; "
                    "catalogue cleanup cannot run"
                )
            if "OutOfMemoryError" in output or completed.returncode == 3:
                raise CatalogueHeapError(output[-500:])
            raise CatalogueCommandError(output[-500:])
        return output
    finally:
        script_path.unlink(missing_ok=True)


def _read_count(statements: Sequence[str]) -> int:
    for statement in statements:
        path = _csv_path(statement)
        if path is not None and "COUNT(*)" in statement.upper():
            return parse_count(path.read_text(encoding="utf-8"))
    raise CatalogueSchemaError("cleanup did not count the remaining rows")


def apply_catalogue_pass(
    *,
    database_file: Path,
    protected_ids: Iterable[int],
    max_age_hours: int,
    execute: Callable[[Sequence[str], float], str],
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Delete unneeded manga in batches. Compact only when the pass finishes.

    ``execute`` runs one script and lets ``CSVWRITE`` land on the paths in
    the statements. A schema we do not recognise, or a chapter scan that
    runs out of memory, deletes nothing.
    """

    if not database_file.is_file():
        return {"skipped": "no database"}
    size = database_file.stat().st_size
    budget = (
        LARGE_PASS_BUDGET_SECONDS
        if size > LARGE_DATABASE_BYTES
        else SMALL_PASS_BUDGET_SECONDS
    )
    deadline = clock() + budget
    with tempfile.TemporaryDirectory(prefix="tankarr-suwayomi-") as temporary:
        schema_csv = Path(temporary) / "schema.csv"
        execute(
            [csvwrite(schema_csv, SCHEMA_QUERY)],
            min(ROUND_TIMEOUT_SECONDS, budget),
        )
        columns = parse_columns(schema_csv.read_text(encoding="utf-8"))
        manga_table = resolve_table(columns, MANGA_REQUIRED, "mangatable")
        if manga_table is None:
            raise CatalogueSchemaError("Suwayomi manga table was not found")
        chapter_table = resolve_table(columns, frozenset({"manga"}), "chaptertable")
        manga_fields = columns[manga_table]
        cutoff = int(now()) - int(max_age_hours) * 3600
        prepare = [
            "DROP TABLE IF EXISTS TANKARR_KEEP;",
            "CREATE TABLE TANKARR_KEEP(ID BIGINT PRIMARY KEY);",
            *keep_id_statements(protected_ids),
            timestamp_keep_sql(manga_table, manga_fields, cutoff),
        ]
        if chapter_table is not None:
            prepare.append(chapter_keep_sql(chapter_table, columns[chapter_table]))
        count_csv = Path(temporary) / "count.csv"
        prepare.append(csvwrite(count_csv, count_query(manga_table, manga_fields)))
        try:
            execute(prepare, min(ROUND_TIMEOUT_SECONDS, max(1.0, deadline - clock())))
        except CatalogueHeapError:
            # The chapter scan is in this script and no delete has run.
            _drop_keep(execute)
            return {
                "deleted": 0,
                "remaining": None,
                "compacted": False,
                "aborted": "chapter scan ran out of memory",
            }
        remaining = _read_count(prepare)
        started = remaining
        deleted = 0
        batch = BATCH_ROWS
        aborted = None
        while remaining > 0 and clock() < deadline:
            repeats = min(
                STATEMENTS_PER_ROUND, max(1, (remaining + batch - 1) // batch)
            )
            script = [delete_sql(manga_table, manga_fields, batch)] * repeats
            script.append(csvwrite(count_csv, count_query(manga_table, manga_fields)))
            try:
                execute(
                    script, min(ROUND_TIMEOUT_SECONDS, max(1.0, deadline - clock()))
                )
            except CatalogueHeapError:
                if batch == 1:
                    aborted = "a single-row delete ran out of memory"
                    break
                batch = 1
                continue
            remaining = _read_count(script)
            deleted = started - remaining
        compacted = False
        if remaining == 0 and deleted > 0 and aborted is None:
            execute(
                ["DROP TABLE IF EXISTS TANKARR_KEEP;", "SHUTDOWN COMPACT;"],
                COMPACT_TIMEOUT_SECONDS,
            )
            compacted = True
        else:
            _drop_keep(execute)
        return {
            "deleted": deleted,
            "remaining": remaining,
            "compacted": compacted,
            "database_bytes": size,
            "budget_seconds": budget,
            "aborted": aborted,
        }


def _drop_keep(execute: Callable[[Sequence[str], float], str]) -> None:
    try:
        execute(["DROP TABLE IF EXISTS TANKARR_KEEP;"], 30)
    except CatalogueError:
        logger.warning("Could not drop the temporary Suwayomi keep table")


async def run_while_stopped(
    runtime: Any, work: Callable[[], Any], *, restart: bool | None = None
) -> Any:
    """Stop a managed server for ``work``, then start it only if it was up.

    The catalogue flag makes ``start`` refuse until this clears it, including
    the supervisor's own restart. The flag is cleared before the restart so
    our own ``start`` is the one that proceeds. The lock around ``stop`` and
    ``start`` is not held across ``work``.

    ``restart=False`` leaves the server stopped (idle shutdown, or it was
    already down). A caller waiting in ``retain`` still gets a start.
    """

    was_running = bool(runtime.running)
    runtime._catalogue_cleanup = True
    try:
        if was_running:
            await runtime.stop()
        result = work()
        if inspect.isawaitable(result):
            result = await result
        return result
    finally:
        waiting = int(getattr(runtime, "_users", 0) or 0) > 0
        should_restart = was_running if restart is None else bool(restart)
        if waiting:
            should_restart = True
        runtime._catalogue_cleanup = False
        if should_restart:
            try:
                await runtime.start()
            finally:
                runtime._catalogue_cleanup = False
