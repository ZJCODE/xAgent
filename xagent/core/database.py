"""The single, non-migrating SQLite schema for one Agent."""
from __future__ import annotations

import sqlite3
from pathlib import Path


STATE_SCHEMA_VERSION = 2

STATE_TABLE_COLUMNS: dict[str, set[str]] = {
    "runtime_meta": {"key", "value"},
    "runtime_events": {
        "sequence",
        "event_id",
        "kind",
        "source",
        "conversation_id",
        "speaker_id",
        "audience_json",
        "content",
        "event_timestamp",
        "metadata_json",
        "status",
        "side_effect_started",
        "result_json",
        "error",
        "created_at",
        "updated_at",
    },
    "deliveries": {
        "delivery_id",
        "event_id",
        "channel",
        "target_json",
        "payload_json",
        "status",
        "attempts",
        "channel_message_id",
        "error",
        "created_at",
        "updated_at",
    },
    "people": {"person_id", "display_name", "created_at", "last_seen_at"},
    "channel_accounts": {
        "channel",
        "account_id",
        "person_id",
        "allow_proactive",
        "last_seen_at",
    },
    "channel_state": {"channel", "state_key", "value_json", "updated_at"},
    "runtime_tasks": {
        "task_id",
        "task_json",
        "status",
        "next_run_at",
        "created_at",
        "updated_at",
    },
    "journal_state": {"key", "value"},
    "messages": {"id", "dedupe_key", "timestamp", "message_json"},
}


def initialize_state_database(path: str | Path) -> Path:
    """Create the complete schema atomically, or validate it without repair."""
    database = Path(path).expanduser().resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(database), timeout=5.0) as connection:
        existing = _table_names(connection)
        if existing:
            _validate_existing(connection, database, existing)
        else:
            connection.executescript(_CREATE_SCHEMA_SQL)
            _validate_existing(connection, database, _table_names(connection))
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
    database.chmod(0o600)
    return database


def _validate_existing(
    connection: sqlite3.Connection,
    path: Path,
    tables: set[str],
) -> None:
    if "runtime_meta" in tables:
        meta_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(runtime_meta)").fetchall()
        }
        if meta_columns == STATE_TABLE_COLUMNS["runtime_meta"]:
            version = connection.execute(
                "SELECT value FROM runtime_meta WHERE key='schema_version'"
            ).fetchone()
            if version is not None and str(version[0]) != str(STATE_SCHEMA_VERSION):
                raise RuntimeError(
                    f"Unsupported runtime schema version {version[0]} at {path}; "
                    f"expected {STATE_SCHEMA_VERSION}. The database was left unchanged."
                )

    expected_tables = set(STATE_TABLE_COLUMNS)
    if tables != expected_tables:
        raise RuntimeError(
            f"Unsupported runtime schema structure at {path}. "
            f"Expected {sorted(expected_tables)}, found {sorted(tables)}. "
            "The database was left unchanged. Point xAgent at a new empty agent directory."
        )

    for table, expected_columns in STATE_TABLE_COLUMNS.items():
        actual = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if actual != expected_columns:
            raise RuntimeError(
                f"Unsupported {table} schema at {path}. "
                f"Expected {sorted(expected_columns)}, found {sorted(actual)}. "
                "The database was left unchanged. Point xAgent at a new empty agent directory."
            )

    version = connection.execute(
        "SELECT value FROM runtime_meta WHERE key='schema_version'"
    ).fetchone()
    if version is None:
        raise RuntimeError(
            f"Runtime schema version is missing at {path}. "
            "The database was left unchanged. Point xAgent at a new empty agent directory."
        )
    if str(version[0]) != str(STATE_SCHEMA_VERSION):
        raise RuntimeError(
            f"Unsupported runtime schema version {version[0]} at {path}; "
            f"expected {STATE_SCHEMA_VERSION}. The database was left unchanged."
        )


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name NOT LIKE 'sqlite_%'
            """
        ).fetchall()
    }


_CREATE_SCHEMA_SQL = f"""
BEGIN IMMEDIATE;

CREATE TABLE runtime_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO runtime_meta(key, value)
VALUES ('schema_version', '{STATE_SCHEMA_VERSION}');

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key TEXT UNIQUE,
    timestamp REAL NOT NULL,
    message_json TEXT NOT NULL
);
CREATE INDEX idx_messages_id ON messages(id);

CREATE TABLE runtime_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    speaker_id TEXT NOT NULL,
    audience_json TEXT NOT NULL,
    content TEXT NOT NULL,
    event_timestamp REAL NOT NULL,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL,
    side_effect_started INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_runtime_events_status_sequence
    ON runtime_events(status, sequence);
CREATE INDEX idx_runtime_events_source_status
    ON runtime_events(source, status);

CREATE TABLE deliveries (
    delivery_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    channel TEXT NOT NULL,
    target_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    channel_message_id TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_deliveries_status_created
    ON deliveries(status, created_at);

CREATE TABLE people (
    person_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    last_seen_at REAL NOT NULL
);
CREATE TABLE channel_accounts (
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    person_id TEXT NOT NULL REFERENCES people(person_id),
    allow_proactive INTEGER NOT NULL DEFAULT 0,
    last_seen_at REAL NOT NULL,
    PRIMARY KEY(channel, account_id)
);
CREATE INDEX idx_channel_accounts_person
    ON channel_accounts(person_id);

CREATE TABLE channel_state (
    channel TEXT NOT NULL,
    state_key TEXT NOT NULL,
    value_json TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(channel, state_key)
);

CREATE TABLE runtime_tasks (
    task_id TEXT PRIMARY KEY,
    task_json TEXT NOT NULL,
    status TEXT NOT NULL,
    next_run_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX idx_runtime_tasks_due
    ON runtime_tasks(status, next_run_at);

CREATE TABLE journal_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

COMMIT;
"""
