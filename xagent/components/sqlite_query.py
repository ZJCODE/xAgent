"""Safe read-only SQLite query helpers for agent-facing SQL tools."""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote


FORBIDDEN_SQL_RE = re.compile(
    r"\b("
    r"PRAGMA|INSERT|UPDATE|DELETE|REPLACE|DROP|CREATE|ALTER|ATTACH|DETACH|"
    r"VACUUM|REINDEX|ANALYZE|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE"
    r")\b",
    re.IGNORECASE,
)


class ReadOnlySQLiteQueryConfig:
    """Limits for read-only SQL query tools."""

    DEFAULT_MAX_ROWS = 50
    HARD_MAX_ROWS = 200
    CONNECT_TIMEOUT = 5.0
    EXECUTION_TIMEOUT_SECONDS = 2.0
    MAX_CELL_CHARS = 2000
    MAX_TOTAL_CHARS = 20000


def execute_readonly_query(
    path: str | Path,
    sql: str,
    *,
    max_rows: int = ReadOnlySQLiteQueryConfig.DEFAULT_MAX_ROWS,
) -> dict[str, Any]:
    """Execute one read-only SELECT/WITH statement against a SQLite database."""
    db_path = Path(path).expanduser()
    normalized_sql = _normalize_readonly_sql(sql)
    normalized_max_rows = _normalize_max_rows(max_rows)

    uri_path = quote(str(db_path), safe="/")
    deadline = time.monotonic() + ReadOnlySQLiteQueryConfig.EXECUTION_TIMEOUT_SECONDS
    uri = f"file:{uri_path}?mode=ro"

    with sqlite3.connect(
        uri,
        uri=True,
        timeout=ReadOnlySQLiteQueryConfig.CONNECT_TIMEOUT,
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0,
            1000,
        )
        cursor = connection.execute(normalized_sql)
        columns = [description[0] for description in cursor.description or []]
        rows = cursor.fetchmany(normalized_max_rows + 1)

    truncated_by_rows = len(rows) > normalized_max_rows
    visible_rows = rows[:normalized_max_rows]

    rendered_rows: list[dict[str, Any]] = []
    total_chars = 0
    truncated_by_size = False
    for row in visible_rows:
        rendered_row: dict[str, Any] = {}
        for column in columns:
            value = _render_cell(row[column])
            total_chars += len(str(value))
            if total_chars > ReadOnlySQLiteQueryConfig.MAX_TOTAL_CHARS:
                truncated_by_size = True
                break
            rendered_row[column] = value
        if truncated_by_size:
            break
        rendered_rows.append(rendered_row)

    return {
        "status": "ok",
        "columns": columns,
        "rows": rendered_rows,
        "row_count": len(rendered_rows),
        "truncated": truncated_by_rows or truncated_by_size,
        "max_rows": normalized_max_rows,
    }


def _normalize_readonly_sql(sql: str) -> str:
    statement = str(sql or "").strip()
    if not statement:
        raise ValueError("SQL query cannot be empty.")

    if statement.endswith(";"):
        statement = statement[:-1].strip()
    statement_without_literals = _strip_single_quoted_literals(statement)
    if ";" in statement_without_literals:
        raise ValueError("Only one SQL statement is allowed.")

    upper_statement = statement.lstrip().upper()
    if not (upper_statement.startswith("SELECT") or upper_statement.startswith("WITH")):
        raise ValueError("Only SELECT or WITH read-only queries are allowed.")

    if "--" in statement or "/*" in statement or "*/" in statement:
        raise ValueError("SQL comments are not allowed in query tools.")

    forbidden = FORBIDDEN_SQL_RE.search(statement_without_literals)
    if forbidden:
        raise ValueError(f"Forbidden SQL keyword: {forbidden.group(1).upper()}.")

    return statement


def _strip_single_quoted_literals(statement: str) -> str:
    """Replace single-quoted SQL string literals before keyword checks."""
    chars: list[str] = []
    index = 0
    in_string = False
    while index < len(statement):
        char = statement[index]
        if not in_string:
            if char == "'":
                in_string = True
                chars.append("''")
            else:
                chars.append(char)
            index += 1
            continue

        if char == "'" and index + 1 < len(statement) and statement[index + 1] == "'":
            index += 2
            continue
        if char == "'":
            in_string = False
        index += 1
    return "".join(chars)


def _normalize_max_rows(max_rows: int) -> int:
    try:
        parsed = int(max_rows)
    except (TypeError, ValueError):
        parsed = ReadOnlySQLiteQueryConfig.DEFAULT_MAX_ROWS
    return max(1, min(parsed, ReadOnlySQLiteQueryConfig.HARD_MAX_ROWS))


def _render_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<bytes {len(value)}>"
    if isinstance(value, str) and len(value) > ReadOnlySQLiteQueryConfig.MAX_CELL_CHARS:
        omitted = len(value) - ReadOnlySQLiteQueryConfig.MAX_CELL_CHARS
        return f"{value[:ReadOnlySQLiteQueryConfig.MAX_CELL_CHARS]}...[truncated {omitted} chars]"
    return value
