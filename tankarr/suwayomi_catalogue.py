"""Keep a managed Suwayomi database limited to what Tankarr still needs.

Suwayomi persists every search hit and every chapter fetch, and the pinned
server has no API that deletes a manga. Listing the whole catalogue through
GraphQL loads every row into a 256 MB heap and crashes the process, so
cleanup never asks GraphQL for the list. It stops the managed server, then
deletes unneeded rows in small SQL batches with a separate, smaller JVM.
Pages, chapter metadata, category links, manga metadata and tracking rows
are deleted before the manga row. The pinned server also cascades those
deletes; the explicit order still works when a foreign key is RESTRICT.

An external Suwayomi server is not rewritten: its database is not ours, and
it has the same lack of a delete API.
"""

from __future__ import annotations

import csv
import inspect
import io
import logging
import math
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


def named_table(columns: dict[str, dict[str, str]], name: str) -> str | None:
    """The table with this name, ignoring case.

    Exposed's ``IntIdTable()`` stores ``ChapterTable`` as ``CHAPTER``. A
    column match is not a name: ``CATEGORYMANGA`` also has a ``MANGA`` column,
    and treating every such table as the chapter table is ambiguous.
    """

    matches = [item for item in columns if item.casefold() == name.casefold()]
    if not matches:
        return None
    if len(matches) != 1:
        raise CatalogueSchemaError(f"ambiguous {name} table: {matches}")
    return matches[0]


# Children of a manga in the pinned Suwayomi schema (v2.4.2366), deleted
# before the manga row. Pages and chapter metadata point at CHAPTER.
# (table, foreign-key column, "chapter" or "manga")
MANGA_CHILD_DELETES: tuple[tuple[str, str, str], ...] = (
    ("PAGE", "chapter", "chapter"),
    ("CHAPTERMETA", "chapter_ref", "chapter"),
    ("CHAPTER", "manga", "manga"),
    ("CATEGORYMANGA", "manga", "manga"),
    ("MANGAMETA", "manga_ref", "manga"),
    ("TRACKRECORD", "manga_id", "manga"),
)


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


def batch_delete_statements(
    columns: dict[str, dict[str, str]],
    manga_table: str,
    manga_fields: dict[str, str],
    chapter_table: str,
    batch: int,
) -> list[str]:
    """Remove one batch of manga ids and the rows that point at them.

    ``TANKARR_BATCH`` already exists. Children go first so a RESTRICT foreign
    key still succeeds. A child table this database does not have is skipped.
    """

    if batch < 1:
        raise CatalogueSchemaError("delete batch must be at least 1")
    chapter_fields = columns[chapter_table]
    chapter_ids = (
        f"SELECT c.{_col(chapter_fields, 'id')} "
        f"FROM {quote_ident(chapter_table)} AS c "
        f"WHERE c.{_col(chapter_fields, 'manga')} IN (SELECT ID FROM TANKARR_BATCH)"
    )
    manga_ids = "SELECT ID FROM TANKARR_BATCH"
    statements = [
        "DELETE FROM TANKARR_BATCH;",
        (
            "INSERT INTO TANKARR_BATCH (ID) "
            f"SELECT m.{_col(manga_fields, 'id')} "
            f"FROM {quote_ident(manga_table)} AS m "
            f"WHERE m.{_col(manga_fields, 'id')} NOT IN (SELECT ID FROM TANKARR_KEEP) "
            f"LIMIT {int(batch)};"
        ),
    ]
    for table_name, column, via in MANGA_CHILD_DELETES:
        if table_name.casefold() == "chapter":
            table = chapter_table
            fields = chapter_fields
        else:
            table = named_table(columns, table_name)
            if table is None:
                continue
            fields = columns[table]
        if column not in fields:
            continue
        if via == "chapter" and "id" not in chapter_fields:
            continue
        target = chapter_ids if via == "chapter" else manga_ids
        statements.append(
            f"DELETE FROM {quote_ident(table)} "
            f"WHERE {_col(fields, column)} IN ({target});"
        )
    statements.append(
        f"DELETE FROM {quote_ident(manga_table)} "
        f"WHERE {_col(manga_fields, 'id')} IN ({manga_ids});"
    )
    return statements


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


def catalogue_limits(size: int) -> tuple[int, int, int]:
    """Pass, round and compact timeouts for a database of ``size`` bytes.

    A file of a few dozen megabytes keeps the short limits. Past that, each
    extra 256 MiB adds time, up to one hour for the pass, twenty minutes for
    one script (the chapter scan) and forty-five minutes for compaction.
    A file of several hundred megabytes needs that scan and ``SHUTDOWN
    COMPACT`` to finish; the short limits kill both.
    """

    if size <= LARGE_DATABASE_BYTES:
        return (
            SMALL_PASS_BUDGET_SECONDS,
            ROUND_TIMEOUT_SECONDS,
            COMPACT_TIMEOUT_SECONDS,
        )
    steps = math.ceil((size - LARGE_DATABASE_BYTES) / (256 * 1024 * 1024))
    return (
        min(LARGE_PASS_BUDGET_SECONDS + steps * 10 * 60, 60 * 60),
        min(ROUND_TIMEOUT_SECONDS + steps * 180, 20 * 60),
        min(COMPACT_TIMEOUT_SECONDS + steps * 600, 45 * 60),
    )


def cleanup_jvm(base: Path) -> tuple[str, ...]:
    """Heap for one cleanup JVM. A large file does not fit in 192 MiB."""

    try:
        size = Path(f"{base}.mv.db").stat().st_size
    except OSError:
        size = 0
    if size >= 512 * 1024 * 1024:
        heap = "-Xmx512m"
    elif size > LARGE_DATABASE_BYTES:
        heap = "-Xmx320m"
    else:
        heap = "-Xmx192m"
    return (heap, *CLEANUP_JVM[1:])


# JDBC base path to the user that last opened it. Process-local: a wrong
# guess only costs one rejected login, then the other user is tried.
_h2_users: dict[str, str] = {}


def _h2_auth_failure(message: str) -> bool:
    folded = message.casefold()
    return "28000" in message or "wrong user name or password" in folded


def _h2_user_candidates(base: Path) -> list[str]:
    """The user that worked here, then a blank user, then ``sa``.

    Suwayomi creates the database with an empty user. Older files, and the
    H2 default, use ``sa``. A wrong user fails before any SQL, with state
    28000, and ``IFEXISTS`` means the attempt does not create a user.
    """

    remembered = _h2_users.get(str(base))
    defaults = ["", "sa"]
    if remembered in defaults:
        return [remembered, *[item for item in defaults if item != remembered]]
    return defaults


def catalogue_failure_message(stored: object) -> str | None:
    """The error or abort text from one stored cleanup or compact result."""

    if not isinstance(stored, dict):
        return None
    result = stored.get("result")
    if not isinstance(result, dict):
        return None
    error = result.get("error") or result.get("aborted")
    if not error:
        return None
    return str(error)[:300]


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


def _run_h2_script(
    java: str,
    jar: Path,
    base: Path,
    script_path: Path,
    user: str,
    timeout: float,
) -> str:
    escaped = str(script_path).replace("'", "''")
    command = [
        java,
        *cleanup_jvm(base),
        "-cp",
        str(jar),
        "org.h2.tools.Shell",
        "-url",
        f"jdbc:h2:{base};IFEXISTS=TRUE",
        "-user",
        user,
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


def execute_h2(
    java: str,
    jar: Path,
    base: Path,
    statements: Sequence[str],
    timeout: float,
) -> str:
    """Run one H2 script. ``IFEXISTS`` refuses to create a new database.

    The user is the one that last opened this file, or a blank user and then
    ``sa``. Only a rejected login (SQL state 28000) tries the next user. A
    lock, a syntax error or a timeout is reported as itself.
    """

    script = "\n".join(statements)
    if "CACHE_SIZE" in script.upper():
        raise CatalogueSchemaError("cleanup must not persist an H2 cache size")
    with tempfile.NamedTemporaryFile(
        "w", suffix=".sql", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(script)
        script_path = Path(handle.name)
    try:
        candidates = _h2_user_candidates(base)
        last_error: CatalogueCommandError | None = None
        for index, user in enumerate(candidates):
            try:
                output = _run_h2_script(java, jar, base, script_path, user, timeout)
            except CatalogueCommandError as exc:
                if _h2_auth_failure(str(exc)) and index + 1 < len(candidates):
                    _h2_users.pop(str(base), None)
                    logger.info("Suwayomi H2 user was rejected; trying the next one")
                    last_error = exc
                    continue
                raise
            _h2_users[str(base)] = user
            return output
        raise last_error or CatalogueCommandError("H2 login failed")
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
    budget, round_timeout, compact_timeout = catalogue_limits(size)
    deadline = clock() + budget
    with tempfile.TemporaryDirectory(prefix="tankarr-suwayomi-") as temporary:
        schema_csv = Path(temporary) / "schema.csv"
        execute(
            [csvwrite(schema_csv, SCHEMA_QUERY)],
            min(round_timeout, budget),
        )
        columns = parse_columns(schema_csv.read_text(encoding="utf-8"))
        manga_table = named_table(columns, "MANGA")
        if manga_table is None or not MANGA_REQUIRED <= set(columns[manga_table]):
            raise CatalogueSchemaError("Suwayomi manga table was not found")
        chapter_table = named_table(columns, "CHAPTER")
        if chapter_table is None:
            raise CatalogueSchemaError("Suwayomi chapter table was not found")
        manga_fields = columns[manga_table]
        cutoff = int(now()) - int(max_age_hours) * 3600
        prepare = [
            "DROP TABLE IF EXISTS TANKARR_BATCH;",
            "DROP TABLE IF EXISTS TANKARR_KEEP;",
            "CREATE TABLE TANKARR_KEEP(ID BIGINT PRIMARY KEY);",
            *keep_id_statements(protected_ids),
            timestamp_keep_sql(manga_table, manga_fields, cutoff),
            chapter_keep_sql(chapter_table, columns[chapter_table]),
        ]
        count_csv = Path(temporary) / "count.csv"
        prepare.append(csvwrite(count_csv, count_query(manga_table, manga_fields)))
        try:
            execute(prepare, min(round_timeout, max(1.0, deadline - clock())))
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
            script = [
                "DROP TABLE IF EXISTS TANKARR_BATCH;",
                "CREATE TABLE TANKARR_BATCH(ID BIGINT PRIMARY KEY);",
            ]
            for _repeat in range(repeats):
                script.extend(
                    batch_delete_statements(
                        columns, manga_table, manga_fields, chapter_table, batch
                    )
                )
            script.append("DROP TABLE IF EXISTS TANKARR_BATCH;")
            script.append(csvwrite(count_csv, count_query(manga_table, manga_fields)))
            try:
                execute(script, min(round_timeout, max(1.0, deadline - clock())))
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
                [
                    "DROP TABLE IF EXISTS TANKARR_BATCH;",
                    "DROP TABLE IF EXISTS TANKARR_KEEP;",
                    "SHUTDOWN COMPACT;",
                ],
                compact_timeout,
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
        execute(
            [
                "DROP TABLE IF EXISTS TANKARR_BATCH;",
                "DROP TABLE IF EXISTS TANKARR_KEEP;",
            ],
            30,
        )
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
