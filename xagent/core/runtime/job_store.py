"""Transactional SQLite storage for local background jobs."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence

from .job_process import process_identity_matches


SCHEMA_VERSION = 1
DATABASE_FILENAME = "jobs.sqlite3"
JOB_KIND_PROCESS = "process"

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_STARTING = "starting"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_CANCELLING = "cancelling"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"
JOB_STATUS_INTERRUPTED = "interrupted"

ACTIVE_JOB_STATUSES = {
    JOB_STATUS_QUEUED,
    JOB_STATUS_STARTING,
    JOB_STATUS_RUNNING,
    JOB_STATUS_CANCELLING,
}
TERMINAL_JOB_STATUSES = {
    JOB_STATUS_SUCCEEDED,
    JOB_STATUS_FAILED,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_INTERRUPTED,
}
ATTENTION_JOB_STATUSES = {JOB_STATUS_FAILED, JOB_STATUS_INTERRUPTED}

DELIVERY_PENDING = "pending"
DELIVERY_DELIVERING = "delivering"
DELIVERY_DELIVERED = "delivered"
DELIVERY_EXPIRED = "expired"

DEFAULT_LOG_TAIL_BYTES = 128 * 1024
MAX_LOG_TAIL_BYTES = 1024 * 1024


class IdempotencyConflict(ValueError):
    """An idempotency key was reused for a different job specification."""


class QueueCapacityError(ValueError):
    """The configured queue capacity has been reached."""


@dataclass(frozen=True)
class JobSettings:
    max_concurrent: int = 2
    max_queued: int = 1000
    worker_idle_seconds: float = 60.0
    runner_heartbeat_seconds: float = 2.0
    runner_stale_seconds: float = 10.0
    cancel_grace_seconds: float = 5.0
    log_segment_bytes: int = 1024 * 1024
    log_segments: int = 4
    retention_days: int = 30
    retention_count: int = 1000

    @classmethod
    def from_mapping(cls, raw: Optional[Mapping[str, Any]]) -> "JobSettings":
        values = dict(raw or {})
        allowed = set(cls.__dataclass_fields__)
        unsupported = sorted(set(values) - allowed)
        if unsupported:
            raise ValueError(f"Unsupported jobs key(s): {', '.join(unsupported)}")
        integer_fields = {
            "max_concurrent",
            "max_queued",
            "log_segment_bytes",
            "log_segments",
            "retention_days",
            "retention_count",
        }
        duration_fields = {
            "worker_idle_seconds",
            "runner_heartbeat_seconds",
            "runner_stale_seconds",
            "cancel_grace_seconds",
        }
        normalized: dict[str, Any] = {}
        for name, value in values.items():
            if name in integer_fields:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"jobs.{name} must be an integer")
                normalized[name] = value
            elif name in duration_fields:
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"jobs.{name} must be a number")
                normalized[name] = float(value)
        settings = cls(**normalized)
        settings.validate()
        return settings

    def validate(self) -> None:
        positive = {
            "max_concurrent": self.max_concurrent,
            "max_queued": self.max_queued,
            "worker_idle_seconds": self.worker_idle_seconds,
            "runner_heartbeat_seconds": self.runner_heartbeat_seconds,
            "runner_stale_seconds": self.runner_stale_seconds,
            "cancel_grace_seconds": self.cancel_grace_seconds,
            "log_segment_bytes": self.log_segment_bytes,
            "log_segments": self.log_segments,
            "retention_days": self.retention_days,
            "retention_count": self.retention_count,
        }
        for name, value in positive.items():
            if isinstance(value, bool) or float(value) <= 0:
                raise ValueError(f"jobs.{name} must be positive")
        if self.runner_stale_seconds <= self.runner_heartbeat_seconds:
            raise ValueError("jobs.runner_stale_seconds must exceed runner_heartbeat_seconds")


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    job_id: str
    channel: str
    target: dict[str, Any]
    attempt_count: int
    expires_at_ms: Optional[int]


@dataclass(frozen=True)
class ClaimedAttempt:
    job_id: str
    attempt_id: str
    runner_token: str


@dataclass(frozen=True)
class JobRecord:
    """Stable domain view of a job and its latest execution attempt."""

    jobs_dir: Path
    data: dict[str, Any]
    attempt: Optional[dict[str, Any]] = None
    deliveries: tuple[dict[str, Any], ...] = ()

    @property
    def path(self) -> Path:
        # Kept as a read-only compatibility property for callers that used the
        # old file-backed record.
        return self.jobs_dir / DATABASE_FILENAME

    @property
    def job_id(self) -> str:
        return str(self.data["id"])

    @property
    def title(self) -> str:
        return str(self.data.get("title") or "")

    @property
    def kind(self) -> str:
        return str(self.data.get("kind") or JOB_KIND_PROCESS)

    @property
    def status(self) -> str:
        return str(self.data.get("state") or JOB_STATUS_QUEUED)

    @property
    def spec(self) -> dict[str, Any]:
        return _json_object(self.data.get("spec_json"))

    @property
    def delivery(self) -> dict[str, Any]:
        return _json_object(self.data.get("delivery_json"))

    @property
    def source(self) -> dict[str, Any]:
        return _json_object(self.data.get("source_json"))

    @property
    def result(self) -> dict[str, Any]:
        return _json_object(self.data.get("result_json"))

    @property
    def delivery_channel(self) -> str:
        return str(self.delivery.get("channel") or "")

    @property
    def delivery_user_id(self) -> str:
        return str(self.delivery.get("user_id") or "")

    @property
    def target(self) -> dict[str, Any]:
        raw = self.delivery.get("target")
        target = dict(raw) if isinstance(raw, dict) else {}
        if self.delivery_channel:
            target.setdefault("channel", self.delivery_channel)
        if self.delivery_user_id:
            target.setdefault("user_id", self.delivery_user_id)
        return target

    @property
    def command(self) -> str:
        command = self.spec.get("command")
        if command:
            return str(command)
        argv = self.spec.get("argv")
        return " ".join(str(item) for item in argv) if isinstance(argv, list) else ""

    @property
    def argv(self) -> list[str]:
        raw = self.spec.get("argv")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    @property
    def resources(self) -> list[str]:
        raw = self.spec.get("resources")
        return [str(item) for item in raw] if isinstance(raw, list) else []

    @property
    def log_dir(self) -> Path:
        return self.jobs_dir / self.job_id

    @property
    def payload(self) -> dict[str, Any]:
        """Compatibility payload used by existing notification adapters."""
        return {
            "id": self.job_id,
            "title": self.title,
            "kind": self.kind,
            "status": self.status,
            "spec": self.spec,
            "delivery": self.delivery,
            "source": self.source,
            "result": self.result,
            "last_error": self.data.get("last_error"),
            "reason": self.data.get("reason"),
        }

    def to_job_view(self, *, log_tail: bool | int = False) -> dict[str, Any]:
        attempt = dict(self.attempt or {})
        execution = {
            "attempt_id": attempt.get("id"),
            "attempt_no": attempt.get("attempt_no"),
            "state": attempt.get("state"),
            "runner_pid": attempt.get("runner_pid"),
            "pid": attempt.get("child_pid"),
            "pgid": attempt.get("child_pgid"),
            "exit_code": attempt.get("exit_code"),
            "signal": attempt.get("exit_signal"),
            "reason": attempt.get("reason"),
            "heartbeat_at": _ms_to_text(attempt.get("heartbeat_at_ms")),
        }
        delivery_views = [
            {
                "delivery_id": item.get("id"),
                "channel": item.get("channel"),
                "state": item.get("state"),
                "attempt_count": item.get("attempt_count"),
                "last_error": item.get("last_error"),
                "delivered_at": _ms_to_text(item.get("delivered_at_ms")),
                "expires_at": _ms_to_text(item.get("expires_at_ms")),
            }
            for item in self.deliveries
        ]
        view: dict[str, Any] = {
            "job_id": self.job_id,
            "title": self.title or "Background job",
            "kind": self.kind,
            "status": self.status,
            "desired_state": self.data.get("desired_state"),
            "command": self.command,
            "argv": self.argv or None,
            "shell": bool(self.spec.get("shell")),
            "cwd": self.spec.get("cwd"),
            "timeout_seconds": self.spec.get("timeout_seconds"),
            "resources": self.resources,
            "channel": self.delivery_channel or "local",
            "user_id": self.delivery_user_id,
            "target": self.target,
            "wait_reason": self.data.get("wait_reason"),
            "execution": {key: value for key, value in execution.items() if value is not None},
            "result": self.result,
            "deliveries": delivery_views,
            "delivery_warning": next(
                (str(item.get("last_error")) for item in self.deliveries if item.get("last_error")),
                None,
            ),
            "retry_of": self.data.get("retry_of"),
            "reason": self.data.get("reason"),
            "last_error": self.data.get("last_error"),
            "created_at": _ms_to_text(self.data.get("created_at_ms")),
            "updated_at": _ms_to_text(self.data.get("updated_at_ms")),
            "started_at": _ms_to_text(self.data.get("started_at_ms")),
            "finished_at": _ms_to_text(self.data.get("finished_at_ms")),
            # Compatibility aliases for the previous UI.
            "completed_at": _ms_to_text(self.data.get("finished_at_ms"))
            if self.status == JOB_STATUS_SUCCEEDED
            else None,
            "failed_at": _ms_to_text(self.data.get("finished_at_ms"))
            if self.status in {JOB_STATUS_FAILED, JOB_STATUS_INTERRUPTED}
            else None,
            "cancelled_at": _ms_to_text(self.data.get("finished_at_ms"))
            if self.status == JOB_STATUS_CANCELLED
            else None,
        }
        if log_tail:
            requested = DEFAULT_LOG_TAIL_BYTES if log_tail is True else int(log_tail)
            requested = max(1, min(requested, MAX_LOG_TAIL_BYTES))
            attempt_id = str(attempt.get("id") or "")
            view["stdout_tail"] = read_rotated_log_tail(
                self.log_dir / attempt_id,
                "stdout",
                requested,
            )
            view["stderr_tail"] = read_rotated_log_tail(
                self.log_dir / attempt_id,
                "stderr",
                requested,
            )
        return view


class JobStore:
    """Small, process-safe transactional store for one agent's jobs."""

    def __init__(
        self,
        jobs_dir: Path | str,
        *,
        workspace_dir: Path | str | None = None,
        settings: Optional[JobSettings] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self.root = Path(jobs_dir).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve()
            if workspace_dir is not None
            else (self.root.parent / "workspace").resolve()
        )
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.settings = settings or JobSettings()
        self.settings.validate()
        self.db_path = self.root / DATABASE_FILENAME
        self.logger = logger_ or logging.getLogger(__name__)
        self._initialize()
        self._warn_legacy_json()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current not in {0, SCHEMA_VERSION}:
                raise RuntimeError(
                    f"Unsupported jobs database schema {current}; expected {SCHEMA_VERSION}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL,
                    desired_state TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    spec_hash TEXT NOT NULL,
                    delivery_json TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    idempotency_scope TEXT,
                    idempotency_key TEXT,
                    retry_of TEXT REFERENCES jobs(id),
                    wait_reason TEXT,
                    reason TEXT,
                    result_json TEXT NOT NULL DEFAULT '{}',
                    last_error TEXT,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    started_at_ms INTEGER,
                    finished_at_ms INTEGER,
                    deadline_at_ms INTEGER,
                    version INTEGER NOT NULL DEFAULT 0
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_idempotency
                ON jobs(idempotency_scope, idempotency_key)
                WHERE idempotency_key IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_jobs_state_created
                ON jobs(state, created_at_ms, id);

                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    attempt_no INTEGER NOT NULL,
                    runner_token TEXT NOT NULL,
                    state TEXT NOT NULL,
                    runner_pid INTEGER,
                    runner_boot_id TEXT,
                    runner_start_identity TEXT,
                    child_pid INTEGER,
                    child_pgid INTEGER,
                    child_boot_id TEXT,
                    child_start_identity TEXT,
                    started_at_ms INTEGER,
                    heartbeat_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    exit_code INTEGER,
                    exit_signal INTEGER,
                    reason TEXT,
                    receipt_path TEXT,
                    UNIQUE(job_id, attempt_no)
                );
                CREATE INDEX IF NOT EXISTS idx_attempts_active
                ON attempts(state, heartbeat_at_ms);

                CREATE TABLE IF NOT EXISTS job_resources (
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    resource TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    PRIMARY KEY(job_id, resource)
                );

                CREATE TABLE IF NOT EXISTS resource_leases (
                    resource TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    attempt_id TEXT NOT NULL REFERENCES attempts(id) ON DELETE CASCADE,
                    acquired_at_ms INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    channel TEXT NOT NULL,
                    target_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at_ms INTEGER NOT NULL,
                    last_error TEXT,
                    delivered_at_ms INTEGER,
                    expires_at_ms INTEGER,
                    UNIQUE(job_id, channel)
                );
                CREATE INDEX IF NOT EXISTS idx_deliveries_due
                ON deliveries(state, next_attempt_at_ms);

                CREATE TABLE IF NOT EXISTS worker_health (
                    name TEXT PRIMARY KEY,
                    pid INTEGER,
                    token TEXT,
                    boot_id TEXT,
                    start_identity TEXT,
                    state TEXT NOT NULL,
                    started_at_ms INTEGER,
                    heartbeat_at_ms INTEGER,
                    last_error TEXT
                );
                """
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()

    def _warn_legacy_json(self) -> None:
        legacy_locations = (
            self.root.glob("*.json"),
            (self.root / "failed").glob("*.json*"),
            (self.root / "archive").rglob("*.json"),
        )
        if any(next(paths, None) is not None for paths in legacy_locations):
            self.logger.warning(
                "Legacy file-backed jobs were found under %s; they are preserved but not migrated.",
                self.root,
            )

    def create_job(
        self,
        *,
        title: str = "",
        argv: Optional[Sequence[str]] = None,
        command: Optional[str] = None,
        shell: bool = False,
        cwd: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        resources: Optional[Sequence[str]] = None,
        channel: str = "",
        target: Optional[Mapping[str, Any]] = None,
        user_id: str = "",
        source: Optional[Mapping[str, Any]] = None,
        idempotency_scope: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        retry_of: Optional[str] = None,
    ) -> JobRecord:
        normalized_argv = _normalize_argv(argv)
        normalized_command = str(command or "").strip()
        if bool(normalized_argv) == bool(normalized_command):
            raise ValueError("provide exactly one of argv or command")
        if normalized_command and not shell:
            raise ValueError("command requires shell=true; prefer argv for non-shell execution")
        if normalized_argv and shell:
            raise ValueError("shell=true cannot be combined with argv")
        if timeout_seconds is not None:
            timeout_seconds = int(timeout_seconds)
            if timeout_seconds <= 0:
                raise ValueError("timeout_seconds must be positive when provided")

        normalized_resources = _normalize_resources(resources)
        resolved_cwd = self._resolve_cwd(cwd)
        spec = {
            "argv": normalized_argv or None,
            "command": normalized_command or None,
            "shell": bool(shell),
            "cwd": str(resolved_cwd),
            "timeout_seconds": timeout_seconds,
            "resources": normalized_resources,
        }
        spec_json = _canonical_json(spec)
        normalized_title = str(title or "").strip()
        delivery = {
            "channel": str(channel or "").strip().lower(),
            "target": dict(target or {}),
            "user_id": str(user_id or "").strip(),
        }
        normalized_source = dict(source or {})
        # Despite the historical column name, this is the hash of every
        # persisted creation parameter. Reusing a key with a different title,
        # delivery target, source, or retry origin must not silently return an
        # unrelated job.
        spec_hash = hashlib.sha256(
            _canonical_json(
                {
                    "title": normalized_title,
                    "spec": spec,
                    "delivery": delivery,
                    "source": normalized_source,
                    "retry_of": retry_of,
                }
            ).encode("utf-8")
        ).hexdigest()
        scope = str(idempotency_scope or "").strip() or None
        key = str(idempotency_key or "").strip() or None
        if key and not scope:
            scope = "global"

        now = now_ms()
        job_id = uuid.uuid4().hex
        with self._write() as connection:
            if key:
                existing = connection.execute(
                    """
                    SELECT * FROM jobs
                    WHERE idempotency_scope = ? AND idempotency_key = ?
                    """,
                    (scope, key),
                ).fetchone()
                if existing is not None:
                    if existing["spec_hash"] != spec_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used for a different job"
                        )
                    existing_id = str(existing["id"])
                    return self._record_from_connection(connection, existing_id)

            queued = int(
                connection.execute(
                    "SELECT COUNT(*) FROM jobs WHERE state = ?",
                    (JOB_STATUS_QUEUED,),
                ).fetchone()[0]
            )
            if queued >= self.settings.max_queued:
                raise QueueCapacityError(
                    f"job queue is full ({self.settings.max_queued} queued jobs)"
                )

            connection.execute(
                """
                INSERT INTO jobs (
                    id, title, kind, state, desired_state, spec_json, spec_hash,
                    delivery_json, source_json, idempotency_scope, idempotency_key,
                    retry_of, wait_reason, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    normalized_title,
                    JOB_KIND_PROCESS,
                    JOB_STATUS_QUEUED,
                    JOB_STATUS_RUNNING,
                    spec_json,
                    spec_hash,
                    _canonical_json(delivery),
                    _canonical_json(normalized_source),
                    scope,
                    key,
                    retry_of,
                    "waiting_for_worker",
                    now,
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO job_resources(job_id, resource, position)
                VALUES (?, ?, ?)
                """,
                [(job_id, name, position) for position, name in enumerate(normalized_resources)],
            )
            record = self._record_from_connection(connection, job_id)
        (self.root / job_id).mkdir(parents=True, exist_ok=True)
        return record

    def get_job(self, job_id: str) -> JobRecord:
        normalized = str(job_id or "").strip()
        if not normalized:
            raise ValueError("job_id is required")
        with self._connect() as connection:
            record = self._record_from_connection(connection, normalized)
            if record is None:
                raise FileNotFoundError(f"job not found: {normalized}")
            return record

    def list_jobs(
        self,
        *,
        scope: str = "active",
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[JobRecord], int]:
        normalized_scope = {
            "current": "current",
            "running": "active",
            "archive": "history",
        }.get(scope, scope)
        if normalized_scope not in {"active", "attention", "history", "current", "all"}:
            raise ValueError("scope must be one of: active, attention, history")
        clauses: list[str] = []
        params: list[Any] = []
        active_marks = ",".join("?" for _ in ACTIVE_JOB_STATUSES)
        terminal_marks = ",".join("?" for _ in TERMINAL_JOB_STATUSES)
        attention_marks = ",".join("?" for _ in ATTENTION_JOB_STATUSES)
        if normalized_scope == "active":
            clauses.append(f"j.state IN ({active_marks})")
            params.extend(sorted(ACTIVE_JOB_STATUSES))
        elif normalized_scope == "history":
            clauses.append(f"j.state IN ({terminal_marks})")
            params.extend(sorted(TERMINAL_JOB_STATUSES))
        elif normalized_scope == "attention":
            clauses.append(
                f"""(
                    j.state IN ({attention_marks})
                    OR EXISTS (
                        SELECT 1 FROM deliveries d
                        WHERE d.job_id = j.id AND d.last_error IS NOT NULL
                    )
                )"""
            )
            params.extend(sorted(ATTENTION_JOB_STATUSES))
        elif normalized_scope == "current":
            clauses.append(
                f"""(
                    j.state IN ({active_marks})
                    OR j.state IN ({attention_marks})
                    OR EXISTS (
                        SELECT 1 FROM deliveries d
                        WHERE d.job_id = j.id AND d.last_error IS NOT NULL
                    )
                )"""
            )
            params.extend(sorted(ACTIVE_JOB_STATUSES))
            params.extend(sorted(ATTENTION_JOB_STATUSES))

        needle = str(query or "").strip().lower()
        if needle:
            clauses.append(
                """(
                    lower(j.id) LIKE ? OR lower(j.title) LIKE ?
                    OR lower(j.spec_json) LIKE ? OR lower(COALESCE(j.last_error, '')) LIKE ?
                )"""
            )
            wildcard = f"%{needle}%"
            params.extend([wildcard, wildcard, wildcard, wildcard])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        normalized_limit = max(1, min(int(limit), 200))
        normalized_offset = max(0, int(offset))
        order = (
            "COALESCE(j.finished_at_ms, j.updated_at_ms) DESC, j.id DESC"
            if normalized_scope in {"history", "attention"}
            else "j.created_at_ms DESC, j.id DESC"
        )
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM jobs j {where}",
                    params,
                ).fetchone()[0]
            )
            rows = connection.execute(
                f"""
                SELECT j.* FROM jobs j
                {where}
                ORDER BY {order}
                LIMIT ? OFFSET ?
                """,
                [*params, normalized_limit, normalized_offset],
            ).fetchall()
            records = [
                self._record_from_connection(connection, str(row["id"]), job_row=row)
                for row in rows
            ]
        return [record for record in records if record is not None], total

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            grouped = {
                str(row["state"]): int(row["count"])
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM jobs GROUP BY state"
                ).fetchall()
            }
            attention = int(
                connection.execute(
                    """
                    SELECT COUNT(DISTINCT j.id)
                    FROM jobs j
                    LEFT JOIN deliveries d ON d.job_id = j.id
                    WHERE j.state IN ('failed', 'interrupted')
                        OR d.last_error IS NOT NULL
                    """
                ).fetchone()[0]
            )
        active = sum(grouped.get(state, 0) for state in ACTIVE_JOB_STATUSES)
        return {
            "active": active,
            "running": active,
            "queued": grouped.get(JOB_STATUS_QUEUED, 0),
            "attention": attention,
            "history": sum(grouped.get(state, 0) for state in TERMINAL_JOB_STATUSES),
            "archive": sum(grouped.get(state, 0) for state in TERMINAL_JOB_STATUSES),
        }

    def request_cancel(self, job_id: str) -> JobRecord:
        now = now_ms()
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"job not found: {job_id}")
            state = str(row["state"])
            if state in TERMINAL_JOB_STATUSES:
                return self._record_from_connection(connection, job_id, job_row=row)
            if state == JOB_STATUS_QUEUED:
                result = {"summary": "Cancelled before start"}
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, desired_state = ?, reason = ?,
                        result_json = ?, wait_reason = NULL, finished_at_ms = ?,
                        updated_at_ms = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        JOB_STATUS_CANCELLED,
                        JOB_STATUS_CANCELLED,
                        "user_cancelled",
                        _canonical_json(result),
                        now,
                        now,
                        job_id,
                    ),
                )
                self._enqueue_delivery(connection, job_id, now)
            else:
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, desired_state = ?, reason = ?,
                        updated_at_ms = ?, version = version + 1
                    WHERE id = ?
                    """,
                    (
                        JOB_STATUS_CANCELLING,
                        JOB_STATUS_CANCELLED,
                        "user_cancelled",
                        now,
                        job_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE attempts SET state = ?
                    WHERE job_id = ? AND state IN ('starting', 'running')
                    """,
                    (JOB_STATUS_CANCELLING, job_id),
                )
            return self._record_from_connection(connection, job_id)

    def retry_job(
        self,
        job_id: str,
        *,
        idempotency_scope: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> JobRecord:
        original = self.get_job(job_id)
        if original.status not in {JOB_STATUS_FAILED, JOB_STATUS_INTERRUPTED}:
            raise ValueError("only failed or interrupted jobs can be retried")
        spec = original.spec
        return self.create_job(
            title=original.title,
            argv=spec.get("argv"),
            command=spec.get("command"),
            shell=bool(spec.get("shell")),
            cwd=spec.get("cwd"),
            timeout_seconds=spec.get("timeout_seconds"),
            resources=spec.get("resources"),
            channel=original.delivery_channel,
            target=original.delivery.get("target"),
            user_id=original.delivery_user_id,
            source={"source": "retry", "retry_of": original.job_id},
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
            retry_of=original.job_id,
        )

    def delete_job(self, job_id: str) -> JobRecord:
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"job not found: {job_id}")
            if str(row["state"]) not in TERMINAL_JOB_STATUSES:
                raise ValueError("cannot delete an active job; cancel it first")
            record = self._record_from_connection(connection, job_id, job_row=row)
            # A retry remains a valid independent job even if its source
            # history entry is explicitly deleted.
            connection.execute(
                "UPDATE jobs SET retry_of = NULL WHERE retry_of = ?",
                (job_id,),
            )
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        shutil.rmtree(self.root / job_id, ignore_errors=True)
        assert record is not None
        return record

    def claim_next(self, *, worker_token: str) -> Optional[ClaimedAttempt]:
        now = now_ms()
        with self._write() as connection:
            active_attempts = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM attempts
                    WHERE state IN ('starting', 'running', 'cancelling')
                    """
                ).fetchone()[0]
            )
            if active_attempts >= self.settings.max_concurrent:
                connection.execute(
                    """
                    UPDATE jobs SET wait_reason = 'concurrency_limit', updated_at_ms = ?
                    WHERE state = ? AND wait_reason != 'concurrency_limit'
                    """,
                    (now, JOB_STATUS_QUEUED),
                )
                return None

            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = ?
                ORDER BY created_at_ms ASC, id ASC
                """,
                (JOB_STATUS_QUEUED,),
            ).fetchall()
            busy = {
                str(row["resource"])
                for row in connection.execute("SELECT resource FROM resource_leases").fetchall()
            }
            blocked_resources: set[str] = set()
            for row in rows:
                job_id = str(row["id"])
                resources = [
                    str(item["resource"])
                    for item in connection.execute(
                        """
                        SELECT resource FROM job_resources
                        WHERE job_id = ? ORDER BY position
                        """,
                        (job_id,),
                    ).fetchall()
                ]
                conflicts = set(resources) & (busy | blocked_resources)
                if conflicts:
                    blocked_resources.update(resources)
                    reason = f"waiting_for_resource:{','.join(sorted(conflicts))}"
                    connection.execute(
                        "UPDATE jobs SET wait_reason = ?, updated_at_ms = ? WHERE id = ?",
                        (reason, now, job_id),
                    )
                    continue

                attempt_no = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM attempts WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                attempt_id = uuid.uuid4().hex
                runner_token = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO attempts (
                        id, job_id, attempt_no, runner_token, state, heartbeat_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        attempt_id,
                        job_id,
                        attempt_no,
                        runner_token,
                        JOB_STATUS_STARTING,
                        now,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO resource_leases(resource, job_id, attempt_id, acquired_at_ms)
                    VALUES (?, ?, ?, ?)
                    """,
                    [(resource, job_id, attempt_id, now) for resource in resources],
                )
                connection.execute(
                    """
                    UPDATE jobs SET state = ?, wait_reason = NULL, updated_at_ms = ?,
                        version = version + 1
                    WHERE id = ? AND state = ?
                    """,
                    (JOB_STATUS_STARTING, now, job_id, JOB_STATUS_QUEUED),
                )
                return ClaimedAttempt(job_id, attempt_id, runner_token)
            return None

    def set_runner_identity(
        self,
        attempt_id: str,
        *,
        runner_token: str,
        pid: int,
        boot_id: str,
        start_identity: Optional[str],
    ) -> None:
        with self._write() as connection:
            connection.execute(
                """
                UPDATE attempts SET runner_pid = ?, runner_boot_id = ?,
                    runner_start_identity = ?, heartbeat_at_ms = ?
                WHERE id = ? AND runner_token = ?
                """,
                (pid, boot_id, start_identity, now_ms(), attempt_id, runner_token),
            )

    def runner_begin(
        self,
        attempt_id: str,
        *,
        runner_token: str,
        runner_pid: int,
        runner_boot_id: str,
        runner_start_identity: Optional[str],
        child_pid: int,
        child_pgid: int,
        child_boot_id: str,
        child_start_identity: Optional[str],
    ) -> JobRecord:
        now = now_ms()
        with self._write() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id = ? AND runner_token = ?",
                (attempt_id, runner_token),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("attempt ownership was lost before process start")
            job_id = str(attempt["job_id"])
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise FileNotFoundError(f"job not found: {job_id}")
            desired = str(job["desired_state"])
            state = (
                JOB_STATUS_CANCELLING
                if desired == JOB_STATUS_CANCELLED
                else JOB_STATUS_RUNNING
            )
            spec = _json_object(job["spec_json"])
            timeout = spec.get("timeout_seconds")
            deadline = now + int(float(timeout) * 1000) if timeout is not None else None
            connection.execute(
                """
                UPDATE attempts SET state = ?, runner_pid = ?, runner_boot_id = ?,
                    runner_start_identity = ?, child_pid = ?, child_pgid = ?,
                    child_boot_id = ?, child_start_identity = ?, started_at_ms = ?,
                    heartbeat_at_ms = ?
                WHERE id = ? AND runner_token = ?
                """,
                (
                    state,
                    runner_pid,
                    runner_boot_id,
                    runner_start_identity,
                    child_pid,
                    child_pgid,
                    child_boot_id,
                    child_start_identity,
                    now,
                    now,
                    attempt_id,
                    runner_token,
                ),
            )
            connection.execute(
                """
                UPDATE jobs SET state = ?, started_at_ms = COALESCE(started_at_ms, ?),
                    deadline_at_ms = COALESCE(deadline_at_ms, ?), updated_at_ms = ?,
                    version = version + 1
                WHERE id = ?
                """,
                (state, now, deadline, now, job_id),
            )
            record = self._record_from_connection(connection, job_id)
            assert record is not None
            return record

    def runner_should_cancel(self, attempt_id: str, *, runner_token: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT j.desired_state
                FROM attempts a JOIN jobs j ON j.id = a.job_id
                WHERE a.id = ? AND a.runner_token = ?
                """,
                (attempt_id, runner_token),
            ).fetchone()
            return row is None or str(row["desired_state"]) == JOB_STATUS_CANCELLED

    def heartbeat_attempt(self, attempt_id: str, *, runner_token: str) -> None:
        with self._write() as connection:
            changed = connection.execute(
                """
                UPDATE attempts SET heartbeat_at_ms = ?
                WHERE id = ? AND runner_token = ?
                    AND state IN ('starting', 'running', 'cancelling')
                """,
                (now_ms(), attempt_id, runner_token),
            ).rowcount
            if changed == 0:
                raise RuntimeError("attempt is no longer active")

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        runner_token: str,
        state: str,
        reason: str,
        result: Mapping[str, Any],
        exit_code: Optional[int],
        exit_signal: Optional[int],
        last_error: Optional[str],
        receipt_path: str,
        ended_at_ms: Optional[int] = None,
    ) -> JobRecord:
        if state not in TERMINAL_JOB_STATUSES:
            raise ValueError(f"invalid terminal state: {state}")
        ended = int(ended_at_ms or now_ms())
        with self._write() as connection:
            attempt = connection.execute(
                "SELECT * FROM attempts WHERE id = ? AND runner_token = ?",
                (attempt_id, runner_token),
            ).fetchone()
            if attempt is None:
                raise RuntimeError("attempt ownership was lost")
            job_id = str(attempt["job_id"])
            job = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise FileNotFoundError(f"job not found: {job_id}")
            if str(job["state"]) in TERMINAL_JOB_STATUSES:
                record = self._record_from_connection(connection, job_id, job_row=job)
                assert record is not None
                return record
            if str(job["desired_state"]) == JOB_STATUS_CANCELLED:
                state = JOB_STATUS_CANCELLED
                reason = "user_cancelled"
                last_error = None
                result = {
                    **dict(result),
                    "summary": "Cancelled",
                }
            connection.execute(
                """
                UPDATE attempts SET state = ?, heartbeat_at_ms = ?, ended_at_ms = ?,
                    exit_code = ?, exit_signal = ?, reason = ?, receipt_path = ?
                WHERE id = ? AND runner_token = ?
                """,
                (
                    state,
                    ended,
                    ended,
                    exit_code,
                    exit_signal,
                    reason,
                    receipt_path,
                    attempt_id,
                    runner_token,
                ),
            )
            connection.execute(
                """
                UPDATE jobs SET state = ?, desired_state = ?, reason = ?,
                    result_json = ?, last_error = ?, finished_at_ms = ?,
                    updated_at_ms = ?, wait_reason = NULL, version = version + 1
                WHERE id = ?
                """,
                (
                    state,
                    state,
                    reason,
                    _canonical_json(dict(result)),
                    last_error,
                    ended,
                    ended,
                    job_id,
                ),
            )
            connection.execute(
                "DELETE FROM resource_leases WHERE attempt_id = ?",
                (attempt_id,),
            )
            self._enqueue_delivery(connection, job_id, ended)
            record = self._record_from_connection(connection, job_id)
            assert record is not None
            return record

    def finalize_without_process(
        self,
        attempt_id: str,
        *,
        runner_token: str,
        state: str,
        reason: str,
        summary: str,
        receipt_path: str = "",
    ) -> JobRecord:
        return self.finish_attempt(
            attempt_id,
            runner_token=runner_token,
            state=state,
            reason=reason,
            result={"summary": summary},
            exit_code=None,
            exit_signal=None,
            last_error=summary if state in ATTENTION_JOB_STATUSES else None,
            receipt_path=receipt_path,
        )

    def reconcile_receipts(self) -> int:
        recovered = 0
        with self._connect() as connection:
            active = connection.execute(
                """
                SELECT a.id, a.runner_token, a.job_id
                FROM attempts a JOIN jobs j ON j.id = a.job_id
                WHERE j.state IN ('starting', 'running', 'cancelling')
                """
            ).fetchall()
        for row in active:
            attempt_dir = self.root / str(row["job_id"]) / str(row["id"])
            receipt = attempt_dir / "result.json"
            if not receipt.is_file():
                continue
            try:
                payload = json.loads(receipt.read_text(encoding="utf-8"))
                self.finish_attempt(
                    str(row["id"]),
                    runner_token=str(row["runner_token"]),
                    state=str(payload["state"]),
                    reason=str(payload.get("reason") or "receipt_recovery"),
                    result=dict(payload.get("result") or {}),
                    exit_code=payload.get("exit_code"),
                    exit_signal=payload.get("exit_signal"),
                    last_error=payload.get("last_error"),
                    receipt_path=str(receipt),
                    ended_at_ms=payload.get("ended_at_ms"),
                )
                recovered += 1
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                self.logger.exception("Failed to reconcile job receipt %s", receipt)
        return recovered

    def stale_attempts(self, *, stale_before_ms: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT a.*, j.desired_state AS job_desired_state
                FROM attempts a JOIN jobs j ON j.id = a.job_id
                WHERE a.state IN ('starting', 'running', 'cancelling')
                    AND a.heartbeat_at_ms < ?
                """,
                (stale_before_ms,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_stale_attempt_terminal(
        self,
        attempt_id: str,
        *,
        runner_token: str,
        cancelled: bool,
    ) -> JobRecord:
        if cancelled:
            return self.finalize_without_process(
                attempt_id,
                runner_token=runner_token,
                state=JOB_STATUS_CANCELLED,
                reason="user_cancelled",
                summary="Cancelled before the runner became available",
            )
        return self.finalize_without_process(
            attempt_id,
            runner_token=runner_token,
            state=JOB_STATUS_INTERRUPTED,
            reason="runner_lost",
            summary="Execution was interrupted; the result could not be verified",
        )

    def set_worker_health(
        self,
        *,
        pid: int,
        token: str,
        boot_id: str,
        start_identity: Optional[str],
        state: str,
        last_error: Optional[str] = None,
        started_at_ms: Optional[int] = None,
    ) -> None:
        now = now_ms()
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO worker_health(
                    name, pid, token, boot_id, start_identity, state,
                    started_at_ms, heartbeat_at_ms, last_error
                ) VALUES ('dispatcher', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    pid = excluded.pid,
                    token = excluded.token,
                    boot_id = excluded.boot_id,
                    start_identity = excluded.start_identity,
                    state = excluded.state,
                    started_at_ms = COALESCE(worker_health.started_at_ms, excluded.started_at_ms),
                    heartbeat_at_ms = excluded.heartbeat_at_ms,
                    last_error = excluded.last_error
                """,
                (
                    pid,
                    token,
                    boot_id,
                    start_identity,
                    state,
                    started_at_ms or now,
                    now,
                    last_error,
                ),
            )

    def worker_health(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM worker_health WHERE name = 'dispatcher'"
            ).fetchone()
        if row is None:
            return {
                "state": "stopped",
                "available": False,
                "last_heartbeat_at": None,
            }
        data = dict(row)
        heartbeat = int(data.get("heartbeat_at_ms") or 0)
        fresh = heartbeat >= now_ms() - int(self.settings.runner_stale_seconds * 1000)
        pid = int(data.get("pid") or 0)
        identity_matches = process_identity_matches(
            pid,
            expected_boot_id=str(data.get("boot_id") or ""),
            expected_start_identity=str(data.get("start_identity") or ""),
        )
        return {
            "state": data.get("state") if fresh else "stale",
            "available": (
                fresh
                and identity_matches
                and data.get("state") in {"starting", "running", "idle"}
            ),
            "pid": pid or None,
            "token": data.get("token"),
            "boot_id": data.get("boot_id"),
            "start_identity": data.get("start_identity"),
            "last_error": data.get("last_error"),
            "started_at": _ms_to_text(data.get("started_at_ms")),
            "last_heartbeat_at": _ms_to_text(heartbeat),
        }

    def claim_deliveries(
        self,
        channels: Sequence[str],
        *,
        limit: int = 10,
    ) -> list[DeliveryRecord]:
        normalized = sorted({str(item or "").strip().lower() for item in channels})
        if not normalized:
            return []
        now = now_ms()
        marks = ",".join("?" for _ in normalized)
        claimed: list[DeliveryRecord] = []
        with self._write() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM deliveries
                WHERE channel IN ({marks})
                    AND state IN (?, ?)
                    AND next_attempt_at_ms <= ?
                ORDER BY next_attempt_at_ms, id
                LIMIT ?
                """,
                [
                    *normalized,
                    DELIVERY_PENDING,
                    DELIVERY_DELIVERING,
                    now,
                    max(1, min(int(limit), 50)),
                ],
            ).fetchall()
            for row in rows:
                expires = row["expires_at_ms"]
                if expires is not None and int(expires) <= now:
                    connection.execute(
                        "UPDATE deliveries SET state = ?, last_error = NULL WHERE id = ?",
                        (DELIVERY_EXPIRED, row["id"]),
                    )
                    continue
                attempts = int(row["attempt_count"] or 0) + 1
                connection.execute(
                    """
                    UPDATE deliveries SET state = ?, attempt_count = ?,
                        next_attempt_at_ms = ?
                    WHERE id = ?
                    """,
                    (DELIVERY_DELIVERING, attempts, now + 30_000, row["id"]),
                )
                claimed.append(
                    DeliveryRecord(
                        delivery_id=str(row["id"]),
                        job_id=str(row["job_id"]),
                        channel=str(row["channel"]),
                        target=_json_object(row["target_json"]),
                        attempt_count=attempts,
                        expires_at_ms=int(expires) if expires is not None else None,
                    )
                )
        return claimed

    def delivery_succeeded(self, delivery_id: str) -> None:
        with self._write() as connection:
            connection.execute(
                """
                UPDATE deliveries SET state = ?, delivered_at_ms = ?,
                    last_error = NULL
                WHERE id = ?
                """,
                (DELIVERY_DELIVERED, now_ms(), delivery_id),
            )

    def delivery_failed(self, delivery_id: str, error: str, *, attempt_count: int) -> None:
        delay_seconds = min(300.0, 2.0 ** min(max(attempt_count, 1), 8))
        jitter_ms = int(uuid.uuid4().int % 1000)
        with self._write() as connection:
            connection.execute(
                """
                UPDATE deliveries SET state = ?, next_attempt_at_ms = ?,
                    last_error = ?
                WHERE id = ?
                """,
                (
                    DELIVERY_PENDING,
                    now_ms() + int(delay_seconds * 1000) + jitter_ms,
                    _safe_error(error),
                    delivery_id,
                ),
            )

    def cleanup_retention(self) -> int:
        cutoff = now_ms() - int(self.settings.retention_days * 86_400_000)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, finished_at_ms FROM jobs
                WHERE state IN ('succeeded', 'failed', 'cancelled', 'interrupted')
                ORDER BY COALESCE(finished_at_ms, updated_at_ms) DESC
                """
            ).fetchall()
        delete_ids = {
            str(row["id"])
            for index, row in enumerate(rows)
            if index >= self.settings.retention_count
            or int(row["finished_at_ms"] or 0) < cutoff
        }
        for job_id in delete_ids:
            try:
                self.delete_job(job_id)
            except (FileNotFoundError, ValueError):
                continue
        return len(delete_ids)

    def _enqueue_delivery(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        created_at_ms: int,
    ) -> None:
        row = connection.execute(
            "SELECT delivery_json FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return
        delivery = _json_object(row["delivery_json"])
        channel = str(delivery.get("channel") or "local").strip().lower() or "local"
        target = delivery.get("target")
        normalized_target = dict(target) if isinstance(target, dict) else {}
        user_id = str(delivery.get("user_id") or "").strip()
        if user_id:
            normalized_target.setdefault("user_id", user_id)
        expires = created_at_ms + 10 * 60 * 1000 if channel == "voice" else None
        connection.execute(
            """
            INSERT OR IGNORE INTO deliveries(
                id, job_id, channel, target_json, state,
                next_attempt_at_ms, expires_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                job_id,
                channel,
                _canonical_json(normalized_target),
                DELIVERY_PENDING,
                created_at_ms,
                expires,
            ),
        )

    def _resolve_cwd(self, raw: Optional[str]) -> Path:
        candidate = Path(str(raw).strip()).expanduser() if raw else self.workspace_dir
        if not candidate.is_absolute():
            candidate = self.workspace_dir / candidate
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.workspace_dir)
        except ValueError as exc:
            raise ValueError("job cwd must stay inside the agent workspace") from exc
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _record_from_connection(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        job_row: Optional[sqlite3.Row] = None,
    ) -> Optional[JobRecord]:
        row = job_row or connection.execute(
            "SELECT * FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        attempt = connection.execute(
            """
            SELECT * FROM attempts WHERE job_id = ?
            ORDER BY attempt_no DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
        deliveries = connection.execute(
            "SELECT * FROM deliveries WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return JobRecord(
            jobs_dir=self.root,
            data=dict(row),
            attempt=dict(attempt) if attempt is not None else None,
            deliveries=tuple(dict(item) for item in deliveries),
        )


def now_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_object(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}


def _normalize_argv(raw: Optional[Sequence[str]]) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        raise ValueError("argv must be a list of strings")
    values = [str(item) for item in raw]
    if not values or not values[0].strip():
        raise ValueError("argv must contain a non-empty executable")
    if len(values) > 256:
        raise ValueError("argv may contain at most 256 items")
    return values


def _normalize_resources(raw: Optional[Sequence[str]]) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)):
        raise ValueError("resources must be a list of names")
    seen: set[str] = set()
    normalized: list[str] = []
    for item in raw:
        name = str(item).strip()
        if not name or name in seen:
            continue
        if len(name) > 128:
            raise ValueError("resource names may contain at most 128 characters")
        seen.add(name)
        normalized.append(name)
    if len(normalized) > 32:
        raise ValueError("a job may request at most 32 resources")
    return normalized


def _safe_error(value: Any) -> str:
    text = " ".join(str(value or "delivery failed").split()).strip()
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return text[:500]


def read_rotated_log_tail(
    attempt_dir: Path,
    stream_name: str,
    max_bytes: int,
) -> str:
    if not attempt_dir.is_dir():
        return ""
    paths = sorted(attempt_dir.glob(f"{stream_name}.*.log"))
    if not paths:
        return ""
    remaining = max(1, int(max_bytes))
    chunks: list[bytes] = []
    for path in reversed(paths):
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                if size > remaining:
                    handle.seek(size - remaining)
                data = handle.read()
        except OSError:
            continue
        chunks.append(data)
        remaining -= len(data)
        if remaining <= 0:
            break
    return b"".join(reversed(chunks)).decode("utf-8", errors="replace")
