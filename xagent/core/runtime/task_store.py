"""Transactional task definitions, occurrences, attempts and delivery receipts.

Only this module accesses task persistence. The runtime owns execution; SQLite
transactions protect scheduling and management operations.
"""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
import threading
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .scheduler import calculate_next_recurrence_run_at, normalize_recurrence_rules
from .task_receipts import sync_task_directory

_initialization_lock = threading.Lock()
UTC = timezone.utc
TERMINAL = {"succeeded", "failed", "needs_review", "skipped"}


def system_timezone() -> str:
    candidates = [os.environ.get("TZ", "").removeprefix(":")]
    try:
        candidates.append(str(Path("/etc/localtime").resolve()).split("zoneinfo/")[-1])
        candidates.append(Path("/etc/timezone").read_text().strip())
    except OSError:
        pass
    for name in candidates:
        try:
            ZoneInfo(name)
            return name
        except (ValueError, KeyError):
            pass
    return "UTC"


def validate_timezone(name):
    try:
        ZoneInfo(name)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Unknown IANA timezone: {name}") from exc
    return name


def aware(value: datetime | str, tz: str = "UTC") -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return (dt.replace(tzinfo=ZoneInfo(tz)) if dt.tzinfo is None else dt).astimezone(UTC).replace(microsecond=0)


def stamp(value: datetime) -> str:
    return aware(value).isoformat()


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def next_due(payload: dict, due: datetime) -> datetime | None:
    """Calendar recurrences use frozen wall time; intervals use elapsed time."""
    rules = normalize_recurrence_rules(payload.get("recurrence"))
    if not rules:
        return None
    tz = payload["timezone"]
    if rules[0]["kind"] == "interval":
        candidate = due + timedelta(seconds=rules[0]["every_seconds"])
        return candidate if candidate <= aware(rules[0]["end_at"], tz) else None
    wall = due.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    while True:
        wall = calculate_next_recurrence_run_at(rules, now=wall, current_run_at=wall)
        if wall is None:
            return None
        candidate = aware(wall, tz)
        # Skip nonexistent DST wall times. A repeated wall time fires once,
        # at fold=0, rather than dispatching twice during the clock rollback.
        if candidate > due and candidate.astimezone(ZoneInfo(tz)).replace(tzinfo=None) == wall:
            return candidate


def deadline(payload, due):
    end = due + timedelta(seconds=payload.get("execution", {}).get("max_lateness_seconds", 86400))
    rules = payload.get("recurrence") or []
    if rules and rules[0]["kind"] == "interval":
        end = min(end, aware(rules[0]["end_at"], payload["timezone"]))
    return end


SCHEMA = """
PRAGMA user_version=1;
CREATE TABLE tasks(id TEXT PRIMARY KEY, payload TEXT NOT NULL, next_due TEXT,
 status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE runs(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
 due_at TEXT NOT NULL, status TEXT NOT NULL, phase TEXT NOT NULL, data TEXT NOT NULL,
 UNIQUE(task_id, due_at));
CREATE INDEX runs_ready ON runs(status, due_at);
CREATE TABLE attempts(id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 data TEXT NOT NULL);
CREATE TABLE deliveries(run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
 part_id TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(run_id, part_id));
"""


TASK_FIELDS = {"timezone", "id", "kind", "title", "status", "task", "delivery", "execution", "source", "created_at", "updated_at", "run_at", "recurrence", "paused_at", "completed_at", "completion_reason"}


class TaskStorageError(RuntimeError):
    pass


class TaskStore:
    def __init__(self, tasks_dir: Path | str):
        self.root = Path(tasks_dir).expanduser().resolve()
        self.path = self.root / "tasks.sqlite3"
        with _initialization_lock:
            if not self.path.exists():
                from .ownership import RuntimeOwnership, runtime_owned_here
                ownership = nullcontext() if runtime_owned_here(self.root.parent) else RuntimeOwnership(self.root.parent)
                with ownership:
                    if not self.path.exists():
                        self._initialize()
        with self.connect() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
            if row[0] != 1:
                raise TaskStorageError("Unsupported or incomplete task database; scheduler has not started")

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path.as_uri() + "?mode=rw", timeout=10, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        sync_task_directory(self.root.parent)
        sync_task_directory(self.root.parent.parent)
        temporary = self.root / f".tasks-{uuid.uuid4().hex}.sqlite3"
        connection = sqlite3.connect(temporary)
        try:
            with connection:
                connection.executescript(SCHEMA)
            connection.close()
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            sync_task_directory(self.root)
        finally:
            connection.close()
            temporary.unlink(missing_ok=True)

    def insert_task(self, payload):
        payload = {key: value for key, value in payload.items() if key in TASK_FIELDS}
        due = aware(payload["run_at"], payload["timezone"])
        with self.transaction() as c:
            c.execute("INSERT INTO tasks(id,payload,next_due,status) VALUES(?,?,?,?)",
                      (payload["id"], encode(payload), stamp(due), payload.get("status", "active")))

    def tasks(self, include_completed=False):
        with self.connect() as c:
            rows = c.execute("SELECT * FROM tasks ORDER BY next_due,id").fetchall()
            views = [self.task_view(c, row) for row in rows]
            return [p for p in views if include_completed or p["status"] != "completed" or p["unresolved_runs"]
                    or p.get("last_run_status") == "failed" and not p.get("last_run_resolved")]

    def task(self, task_id):
        with self.connect() as c:
            row = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise FileNotFoundError(f"task not found: {task_id}")
            return self.task_view(c, row)

    @staticmethod
    def task_view(c, row):
        payload = json.loads(row["payload"])
        payload.update(status=row["status"], revision=row["revision"], next_due=row["next_due"])
        if row["next_due"]:
            payload["run_at"] = aware(row["next_due"]).astimezone(ZoneInfo(payload["timezone"])).isoformat()
        latest = c.execute("SELECT data FROM runs WHERE task_id=? ORDER BY due_at DESC LIMIT 1", (row["id"],)).fetchone()
        reviews = c.execute("SELECT id FROM runs WHERE task_id=? AND status='needs_review' AND json_extract(data,'$.resolved_at') IS NULL", (row["id"],)).fetchall()
        payload["unresolved_runs"] = [item[0] for item in reviews]
        if latest:
            run = json.loads(latest[0])
            payload.update(last_run={"run_id": run["id"], "attempt_id": run.get("attempt_id"),
                                     "stage": run["status"], "failure_stage": run.get("failure_stage"), "due_at": run["due_at"]},
                           last_run_status=run["status"], last_run_at=run.get("started_at"),
                           last_error=run.get("error"), failed_at=run.get("finished_at") if run.get("error") else None,
                           next_retry_at=run.get("retry_at"), last_run_resolved=bool(run.get("resolved_at")))
        payload["blocked_on_review"] = bool(payload.get("execution", {}).get("block_on_uncertain") and reviews)
        payload["running"] = bool(c.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('executing','delivering')", (row["id"],)).fetchone())
        return payload

    def update_task(self, task_id, payload, revision):
        payload = {key: value for key, value in payload.items() if key in TASK_FIELDS}
        with self.transaction() as c:
            if c.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('executing','delivering')", (task_id,)).fetchone():
                raise ValueError("Task is currently running; retry the edit when it finishes")
            existing = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if existing is None:
                raise FileNotFoundError("Task not found")
            old = json.loads(existing["payload"])
            desired = stamp(aware(payload["run_at"], payload["timezone"])) if payload.get("status") in {"active", "paused"} else None
            if existing["next_due"] is None and desired == stamp(aware(old["run_at"], old["timezone"])):
                desired = None
            rescheduled = desired != existing["next_due"] or payload.get("recurrence") != old.get("recurrence") or payload["timezone"] != old["timezone"]
            cursor = c.execute("UPDATE tasks SET payload=?,next_due=?,status=?,revision=revision+1 WHERE id=? AND revision=?",
                 (encode(payload), desired,
                  payload["status"], task_id, revision))
            if cursor.rowcount != 1:
                raise ValueError("Task changed while editing; refresh and retry")
            for row in c.execute("SELECT data FROM runs WHERE task_id=? AND status IN ('pending','waiting_channel','retry_wait','result_ready')", (task_id,)).fetchall():
                run = json.loads(row[0])
                if rescheduled:
                    run.update(status="skipped", error="Task was rescheduled before this execution resumed", finished_at=payload["updated_at"])
                else:
                    if not run.get("result"):
                        run["snapshot"] = payload
                    else:
                        run["snapshot"]["execution"] = payload.get("execution", {})
                    run["expires_at"] = stamp(deadline(payload, aware(run["due_at"])))
                self.put_run(c, run)

    def delete_task(self, task_id):
        with self.transaction() as c:
            if c.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('executing','delivering')", (task_id,)).fetchone():
                raise ValueError("Cannot delete a running task")
            c.execute("DELETE FROM tasks WHERE id=?", (task_id,))

    @staticmethod
    def put_run(c, run):
        c.execute("UPDATE runs SET status=?,phase=?,data=? WHERE id=?", (run["status"], run["phase"], encode(run), run["id"]))

    def get_run(self, task_id, run_id):
        with self.connect() as c:
            row = c.execute("SELECT data FROM runs WHERE task_id=? AND id=?", (task_id, run_id)).fetchone()
            if row is None:
                raise FileNotFoundError("Task execution not found")
            run = json.loads(row[0])
            run["attempts"] = [json.loads(r[0]) for r in c.execute("SELECT data FROM attempts WHERE run_id=? ORDER BY rowid", (run_id,))]
            run["deliveries"] = [json.loads(r[0]) for r in c.execute("SELECT data FROM deliveries WHERE run_id=? ORDER BY rowid", (run_id,))]
            return run

    def list_runs(self, task_id, limit=50, offset=0):
        with self.connect() as c:
            rows = c.execute("SELECT data FROM runs WHERE task_id=? ORDER BY due_at DESC LIMIT ? OFFSET ?", (task_id, limit, offset)).fetchall()
            return [json.loads(r[0]) for r in rows]

    def materialize(self, now):
        """Create newest due occurrence and advance schedule in one transaction."""
        with self.transaction() as c:
            for paused in c.execute("SELECT * FROM tasks WHERE status='paused'").fetchall():
                payload = json.loads(paused["payload"])
                rules = payload.get("recurrence") or []
                if rules and rules[0]["kind"] == "interval" and aware(rules[0]["end_at"], payload["timezone"]) < now:
                    if paused["next_due"]:
                        self._insert_run(c, payload, aware(paused["next_due"]), "skipped", now, error="Interval window ended while paused")
                    for row in c.execute("SELECT data FROM runs WHERE task_id=? AND status IN ('pending','waiting_channel','retry_wait','result_ready')", (paused["id"],)).fetchall():
                        run = json.loads(row[0])
                        run.update(status="skipped", finished_at=stamp(now), error="Interval window ended while paused")
                        self.put_run(c, run)
                    payload.update(completed_at=stamp(now), completion_reason="interval_window_ended")
                    c.execute("UPDATE tasks SET status='completed',next_due=NULL,payload=?,revision=revision+1 WHERE id=?", (encode(payload), paused["id"]))
            for row in c.execute("SELECT * FROM tasks WHERE status='active' AND next_due<=?", (stamp(now),)).fetchall():
                payload = json.loads(row["payload"])
                if payload.get("execution", {}).get("block_on_uncertain") and c.execute(
                    "SELECT 1 FROM runs WHERE task_id=? AND status='needs_review' AND json_extract(data,'$.resolved_at') IS NULL", (row["id"],)).fetchone():
                    continue
                due = aware(row["next_due"])
                first, skipped = due, 0
                future = next_due(payload, due)
                if payload.get("recurrence") and payload["recurrence"][0]["kind"] == "interval" and future and future <= now:
                    seconds = int(payload["recurrence"][0]["every_seconds"])
                    end = aware(payload["recurrence"][0]["end_at"], payload["timezone"])
                    skipped = int((min(now, end) - due).total_seconds()) // seconds
                    due += timedelta(seconds=skipped * seconds)
                    future = next_due(payload, due)
                else:
                    while future and future <= now:
                        skipped += 1
                        due, future = future, next_due(payload, future)
                if skipped:
                    self._insert_run(c, payload, first, "skipped", now, skip_count=skipped,
                                     last_skipped_before=stamp(due), error="Older missed occurrences coalesced into the latest occurrence")
                expires = deadline(payload, due)
                self._insert_run(c, payload, due, "pending" if now <= expires else "skipped", now,
                                 error=None if now <= expires else "Execution expired before it could start")
                c.execute("UPDATE tasks SET next_due=?,revision=revision+1 WHERE id=?",
                          (stamp(future) if future else None, row["id"]))
                if future is None:
                    terminal = c.execute("SELECT data FROM runs WHERE task_id=? ORDER BY due_at DESC LIMIT 1", (row["id"],)).fetchone()
                    run = json.loads(terminal[0])
                    if run["status"] in TERMINAL:
                        self._finish_task(c, run)

    @staticmethod
    def _insert_run(c, payload, due, status, now, **extra):
        run_id = f"{payload['id']}-{due.strftime('%Y%m%dT%H%M%SZ')}"
        run = dict(id=run_id, task_id=payload["id"], due_at=stamp(due), status=status, phase="prepare",
                   snapshot=payload, created_at=stamp(now),
                   expires_at=stamp(deadline(payload, due)),
                   **extra)
        if status in TERMINAL:
            run["finished_at"] = stamp(now)
        c.execute("INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?)", (run_id, payload["id"], stamp(due), status, "prepare", encode(run)))

    def claim(self, now, can_handle=lambda payload: True):
        with self.transaction() as c:
            rows = c.execute("SELECT r.* FROM runs r JOIN tasks t ON t.id=r.task_id WHERE t.status='active' AND r.status IN ('pending','result_ready','retry_wait','waiting_channel') ORDER BY r.due_at").fetchall()
            for row in rows:
                run = json.loads(row["data"])
                if run["status"] != "waiting_channel" and run.get("retry_at") and aware(run["retry_at"]) > now:
                    continue
                if c.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('executing','delivering')", (run["task_id"],)).fetchone():
                    continue
                if now > aware(run["expires_at"]):
                    run.update(status="skipped", finished_at=stamp(now), error="Execution expired before retry or channel recovery")
                    self.put_run(c, run)
                    self._finish_task(c, run)
                    continue
                if not can_handle(run["snapshot"]):
                    if run["status"] != "waiting_channel":
                        run.update(status="waiting_channel", retry_at=None, error="Delivery channel is unavailable")
                        self.put_run(c, run)
                    continue
                if run["snapshot"].get("execution", {}).get("block_on_uncertain") and c.execute(
                    "SELECT 1 FROM runs WHERE task_id=? AND status='needs_review' AND json_extract(data,'$.resolved_at') IS NULL", (run["task_id"],)).fetchone():
                    continue
                # Pending older runs must not accumulate while a channel is down.
                if not run.get("manual_retry") and c.execute("SELECT 1 FROM runs WHERE task_id=? AND due_at>? AND status NOT IN ('skipped')", (run["task_id"], run["due_at"])).fetchone():
                    run.update(status="skipped", finished_at=stamp(now), error="Superseded by a newer occurrence")
                    self.put_run(c, run)
                    continue
                run.update(status="delivering" if run.get("result") else "executing",
                           phase="deliver" if run.get("result") else "prepare",
                           attempt_id=uuid.uuid4().hex, started_at=run.get("started_at") or stamp(now),
                           attempt_started_at=stamp(now), attempt_count=run.get("attempt_count", 0) + 1,
                           retry_at=None, error=None, finished_at=None)
                self.put_run(c, run)
                c.execute("INSERT INTO attempts VALUES(?,?,?)", (run["attempt_id"], run["id"], encode({"id": run["attempt_id"], "started_at": stamp(now), "status": run["status"]})))
                return run
        return None

    def save_run(self, run):
        with self.transaction() as c:
            self.put_run(c, run)
            if run.get("attempt_id"):
                attempt = {key: run.get(key) for key in ("status", "phase", "error", "error_type", "failure_stage", "finished_at")}
                attempt.update(id=run["attempt_id"], started_at=run.get("attempt_started_at"), finished_at=run.get("attempt_finished_at"))
                c.execute("UPDATE attempts SET data=? WHERE id=?", (encode(attempt), run["attempt_id"]))
            if run["status"] in TERMINAL:
                self._finish_task(c, run)

    @staticmethod
    def _finish_task(c, run):
        row = c.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
        if row and row["next_due"] is None and not c.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('pending','executing','delivering','result_ready','retry_wait','waiting_channel')", (run["task_id"],)).fetchone():
            payload = json.loads(row["payload"])
            recurring = bool(payload.get("recurrence"))
            status = "completed" if recurring or run["status"] in {"succeeded", "skipped"} else run["status"]
            if status == "completed":
                reason = ("interval_window_ended" if payload["recurrence"][0]["kind"] == "interval" else "recurrence_exhausted") if recurring else "one_shot_succeeded" if run["status"] == "succeeded" else "expired"
                payload.update(completed_at=run.get("finished_at"), completion_reason=reason)
            else:
                payload.pop("completed_at", None)
                payload.pop("completion_reason", None)
            c.execute("UPDATE tasks SET status=?,payload=?,revision=revision+1 WHERE id=?", (status, encode(payload), run["task_id"]))

    def recover(self, now, run_id=None):
        recovered = 0
        with self.transaction() as c:
            for row in c.execute("SELECT data FROM runs WHERE status IN ('executing','delivering') AND (? IS NULL OR id=?)", (run_id, run_id)).fetchall():
                run = json.loads(row[0])
                parts = [json.loads(r[0]) for r in c.execute("SELECT data FROM deliveries WHERE run_id=?", (run["id"],))]
                unsafe = run.get("unsafe_tools_started", False)
                uncertain_send = any(p["status"] in {"sending", "needs_review"} and p.get("uncertain_on_error", True) for p in parts)
                if run["status"] == "delivering" and not parts:
                    uncertain_send = True  # Non-journalled transports may have sent.
                unknown = (unsafe and not run.get("result")) or uncertain_send
                run.update(status="needs_review" if unknown else "result_ready" if run.get("result") else "pending",
                           failure_stage=run["phase"], error="Runtime interrupted; external outcome is unknown" if unknown else "Runtime interrupted; safe to resume")
                if run.get("attempt_id"):
                    c.execute("UPDATE attempts SET data=? WHERE id=?", (encode({"id": run["attempt_id"], "status": "interrupted", "started_at": run.get("attempt_started_at"), "finished_at": stamp(now), "error": run["error"]}), run["attempt_id"]))
                self.put_run(c, run)
                if run["status"] == "needs_review":
                    run["finished_at"] = stamp(now)
                    self.put_run(c, run)
                    self._finish_task(c, run)
                recovered += 1
        return recovered

    def act_on_run(self, task_id, run_id, action, now):
        with self.transaction() as c:
            row = c.execute("SELECT data FROM runs WHERE id=? AND task_id=?", (run_id, task_id)).fetchone()
            if row is None:
                raise FileNotFoundError("Task execution not found")
            run = json.loads(row[0])
            if run["status"] not in TERMINAL:
                raise ValueError("Execution is still active")
            if action == "resolve":
                run["resolved_at"] = stamp(now)
                c.execute("UPDATE tasks SET status='completed',revision=revision+1 WHERE id=? AND next_due IS NULL AND status IN ('failed','needs_review')", (task_id,))
            elif action in {"prepare", "deliver"}:
                if run["status"] == "succeeded":
                    raise ValueError("A confirmed execution cannot be repeated")
                if now > aware(run["expires_at"]):
                    raise ValueError("Execution has expired; create a fresh task instead")
                if action == "prepare" and (run.get("result") or run.get("unsafe_tools_started") or run["status"] == "needs_review"):
                    raise ValueError("Cannot safely repeat preparation with recorded results or external side effects")
                if action == "deliver":
                    if not run.get("result"):
                        raise ValueError("No prepared result is available")
                    parts = [json.loads(r[0]) for r in c.execute("SELECT data FROM deliveries WHERE run_id=?", (run_id,))]
                    if (run["status"] == "needs_review" and not parts) or any(p["status"] in {"sending", "needs_review"} and p.get("uncertain_on_error", True) for p in parts):
                        raise ValueError("Delivery outcome is unknown; review it without resending")
                run.pop("resolved_at", None)
                run.update(status="result_ready" if action == "deliver" else "pending", retry_at=None, error=None, manual_retry=True)
                c.execute("UPDATE tasks SET status='active',revision=revision+1 WHERE id=? AND status IN ('failed','needs_review','completed')", (task_id,))
            else:
                raise ValueError("Unknown execution action")
            self.put_run(c, run)
        return run

    def delivery(self, run_id, part_id):
        with self.connect() as c:
            row = c.execute("SELECT data FROM deliveries WHERE run_id=? AND part_id=?", (run_id, part_id)).fetchone()
            return json.loads(row[0]) if row else None

    def save_delivery(self, run_id, part):
        with self.transaction() as c:
            c.execute("INSERT INTO deliveries VALUES(?,?,?) ON CONFLICT(run_id,part_id) DO UPDATE SET data=excluded.data", (run_id, part["id"], encode(part)))
