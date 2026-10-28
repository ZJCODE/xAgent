"""Scheduled task API and a small transactional occurrence worker."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
import re
import uuid
from zoneinfo import ZoneInfo
from datetime import timezone
from .task_store import TaskStore, aware, stamp, system_timezone, validate_timezone
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from .task_receipts import DeliveryUncertainError, strict_task_delivery


from .scheduler import (
    align_interval_next_run,
    is_interval_recurrence,
    materialize_interval_recurrence_rules,
    parse_run_at,
    normalize_recurrence_rules,
    resolve_interval_first_run_at,
    resolve_recurrence_run_at,
)


TASK_KIND_TASK = "task"
TASK_TYPE_MESSAGE = "message"
TASK_TYPE_AGENT = "agent"
TASK_STATUS_ACTIVE = "active"
TASK_STATUS_PAUSED = "paused"
TASK_STATUS_COMPLETED = "completed"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_NEEDS_REVIEW = "needs_review"
TASK_STATE_PENDING = "pending"
TASK_STATE_FAILED = "failed"
TASK_STATE_RUNNING = "running"
TASK_STATE_COMPLETED = "completed"
DEFAULT_RUNTIME_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_CONCURRENT_TASK_DISPATCHES = 4
SUPPORTED_TASK_TYPES = {TASK_TYPE_MESSAGE, TASK_TYPE_AGENT}
@dataclass(frozen=True)
class ScheduledDeliveryContext:
    """Current conversation target used by scheduling tools."""

    channel: str
    user_id: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledTaskRecord:
    """A task definition with its latest execution summary."""

    path: Path
    run_at: datetime
    kind: str
    state: str = TASK_STATE_PENDING
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def name(self) -> str:
        return str(self.payload.get("id") or self.path.name)

    @property
    def task_id(self) -> str:
        raw_id = self.payload.get("id") if isinstance(self.payload, dict) else None
        return str(raw_id or self.path.stem)

    @property
    def task(self) -> dict[str, Any]:
        task = self.payload.get("task") if isinstance(self.payload, dict) else None
        return dict(task) if isinstance(task, dict) else {}

    @property
    def task_type(self) -> str:
        return str(self.task.get("type") or "")

    @property
    def content(self) -> str:
        content = str(self.task.get("content") or "")
        due = self.payload.get("scheduled_at")
        if due and self.task_type == "agent":
            local = aware(due).astimezone(ZoneInfo(self.payload["timezone"]))
            content += (f"\n\nSchedule context: this occurrence was scheduled for {local.isoformat()} "
                        f"({self.payload['timezone']}). Interpret relative dates such as yesterday using "
                        "that scheduled date, including when querying tools. Return only the requested content.")
        return content

    @property
    def title(self) -> str:
        return str(self.payload.get("title") or "").strip()

    @property
    def recurrence(self) -> list[dict[str, Any]]:
        return normalize_recurrence_rules(self.payload.get("recurrence"))

    @property
    def delivery(self) -> dict[str, Any]:
        delivery = self.payload.get("delivery") if isinstance(self.payload, dict) else None
        return dict(delivery) if isinstance(delivery, dict) else {}

    @property
    def delivery_channel(self) -> str:
        return str(self.delivery.get("channel") or "")

    @property
    def target(self) -> dict[str, Any]:
        delivery = self.delivery
        target = delivery.get("target")
        result = dict(target) if isinstance(target, dict) else {}
        channel = str(delivery.get("channel") or "")
        user_id = str(delivery.get("user_id") or "")
        if channel:
            result.setdefault("channel", channel)
        if user_id:
            result.setdefault("user_id", user_id)
        return result

    @property
    def delivery_user_id(self) -> str:
        return str(self.delivery.get("user_id") or "")

    @property
    def execution(self) -> dict[str, Any]:
        execution = self.payload.get("execution") if isinstance(self.payload, dict) else None
        return dict(execution) if isinstance(execution, dict) else {}

    @property
    def status(self) -> str:
        return str(self.payload.get("status") or "active")

    @property
    def is_paused(self) -> bool:
        return self.status == TASK_STATUS_PAUSED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "id": self.task_id,
            "kind": self.kind,
            "state": self.state,
            "reason": self.reason,
            "run_at": self.run_at.isoformat(sep=" "),
            "path": str(self.path),
            "payload": self.payload,
        }

    def to_task_view(self) -> dict[str, Any]:
        is_terminal = self.status in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED, TASK_STATUS_NEEDS_REVIEW}
        run = self.payload.get("last_run") or {}
        return {
            "task_id": self.task_id,
            "title": self.title or "Reminder",
            "task_type": self.task_type,
            "content": self.content,
            "next_run_at": None if is_terminal else self.payload.get("next_due"),
            "timezone": self.payload.get("timezone"),
            "execution": self.execution,
            "next_retry_at": self.payload.get("next_retry_at"),
            "blocked_on_review": self.payload.get("blocked_on_review", False),
            "recurrence": self.recurrence or None,
            "status": self.status,
            "state": self.state,
            "reason": self.reason,
            "channel": self.delivery_channel or "local",
            "user_id": self.delivery_user_id,
            "target": self.target,
            "paused_at": self.payload.get("paused_at") if isinstance(self.payload, dict) else None,
            "created_at": self.payload.get("created_at") if isinstance(self.payload, dict) else None,
            "updated_at": self.payload.get("updated_at") if isinstance(self.payload, dict) else None,
            "completed_at": self.payload.get("completed_at") if isinstance(self.payload, dict) else None,
            "failed_at": self.payload.get("failed_at") if isinstance(self.payload, dict) else None,
            "last_run_at": self.payload.get("last_run_at") if isinstance(self.payload, dict) else None,
            "last_run_status": self.payload.get("last_run_status") if isinstance(self.payload, dict) else None,
            "completion_reason": self.payload.get("completion_reason") if isinstance(self.payload, dict) else None,
            "last_error": self.payload.get("last_error") if isinstance(self.payload, dict) else None,
            "run_id": run.get("run_id"),
            "attempt_id": run.get("attempt_id"),
            "run_stage": run.get("stage"),
            "failure_stage": run.get("failure_stage"),
            "scheduled_run_at": run.get("due_at"),
            "unresolved_runs": self.payload.get("unresolved_runs", []),
            "last_review_error": self.payload.get("last_error"),
        }


_delivery_context_var: contextvars.ContextVar[Optional[ScheduledDeliveryContext]] = contextvars.ContextVar(
    "xagent_scheduled_delivery_context",
    default=None,
)


def current_delivery_context() -> Optional[ScheduledDeliveryContext]:
    """Return the conversation delivery context for the current async task."""
    return _delivery_context_var.get()


@contextlib.contextmanager
def scheduled_delivery_context(context: ScheduledDeliveryContext) -> Iterator[None]:
    """Set the current scheduled delivery context for tool calls in this turn."""
    token = _delivery_context_var.set(context)
    try:
        yield
    finally:
        _delivery_context_var.reset(token)


def _local_datetime(value, tz):
    try:
        parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        parsed = parse_run_at(value)
    return aware(parsed, tz).astimezone(ZoneInfo(tz)).replace(tzinfo=None)


def _recurrence_in_timezone(value, tz):
    if value is None:
        return None
    rules = [value] if isinstance(value, dict) else value
    converted = []
    for rule in rules:
        if isinstance(rule, dict) and str(rule.get("kind") or "").strip().lower() == "interval":
            rule = dict(rule)
            for key in ("start_at", "end_at"):
                if rule.get(key):
                    rule[key] = _local_datetime(rule[key], tz).isoformat(sep=" ")
        converted.append(rule)
    return converted


def resolve_scheduled_task_run_at(
    *,
    run_at: Optional[str | datetime] = None,
    delay_seconds: Optional[int] = None,
    recurrence: Any = None,
    now: datetime | None = None,
    timezone_name: str | None = None,
) -> tuple[datetime, list[dict[str, Any]]]:
    """Resolve creation-time schedule parameters into a concrete next run datetime."""
    tz = validate_timezone(timezone_name or system_timezone())
    current = _local_datetime(now, tz) if now is not None else datetime.now(ZoneInfo(tz)).replace(tzinfo=None, microsecond=0)
    normalized_recurrence = materialize_interval_recurrence_rules(_recurrence_in_timezone(recurrence, tz), now=current)
    if normalized_recurrence:
        if run_at is not None:
            raise ValueError("run_at is only supported for one-time tasks; recurring tasks must define time inside recurrence")
        if is_interval_recurrence(normalized_recurrence):
            return (
                resolve_interval_first_run_at(normalized_recurrence[0], now=current, delay_seconds=delay_seconds),
                normalized_recurrence,
            )
        if delay_seconds is not None:
            raise ValueError("delay_seconds is not supported for recurring tasks")
        candidate = resolve_recurrence_run_at(normalized_recurrence, now=current)
        while _local_datetime(candidate, tz) != candidate:
            candidate = resolve_recurrence_run_at(normalized_recurrence, now=candidate)
        return candidate, normalized_recurrence

    if delay_seconds is None and run_at is None:
        raise ValueError("Provide either run_at or delay_seconds.")
    if delay_seconds is not None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be zero or positive.")
        return current + timedelta(seconds=delay_seconds), []
    if run_at:
        try:
            parsed = datetime.fromisoformat(run_at) if isinstance(run_at, str) else run_at
            return aware(parsed, tz).astimezone(ZoneInfo(tz)).replace(tzinfo=None), []
        except ValueError:
            pass
    return parse_run_at(run_at or ""), []


def enqueue_scheduled_task(
    *,
    task_type: str,
    content: str,
    run_at: str | datetime,
    tasks_dir: Path | str,
    channel: str,
    target: dict[str, Any],
    user_id: str = "",
    title: str = "",
    recurrence: Any = None,
    source: Optional[dict[str, Any]] = None,
    execution: Optional[dict[str, Any]] = None,
    timezone_name: str | None = None,
) -> ScheduledTaskRecord:
    """Persist a scheduled task before acknowledging its creation."""
    normalized_type = str(task_type or "").strip().lower()
    if normalized_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of: {', '.join(sorted(SUPPORTED_TASK_TYPES))}")
    normalized_content = content.strip()
    if not normalized_content:
        raise ValueError("scheduled task content must not be empty")
    tz = validate_timezone(timezone_name or system_timezone())
    normalized_recurrence = materialize_interval_recurrence_rules(
        _recurrence_in_timezone(recurrence, tz), now=datetime.now(ZoneInfo(tz)).replace(tzinfo=None, microsecond=0),
    )
    try:
        parsed_run_at = datetime.fromisoformat(run_at) if isinstance(run_at, str) else run_at
    except ValueError:
        parsed_run_at = parse_run_at(run_at)
    if parsed_run_at.tzinfo is None and _local_datetime(parsed_run_at, tz) != parsed_run_at.replace(microsecond=0):
        raise ValueError("Scheduled wall time does not exist in this timezone")
    root = Path(tasks_dir).expanduser().resolve()
    task_id = uuid.uuid4().hex
    payload = {
        "timezone": tz,
        "id": task_id,
        "kind": TASK_KIND_TASK,
        "title": title.strip() if title else "",
        "status": TASK_STATUS_ACTIVE,
        "task": {
            "type": normalized_type,
            "content": normalized_content,
        },
        "delivery": {
            "channel": str(channel or "").strip(),
            "target": dict(target or {}),
            "user_id": user_id,
        },
        "execution": dict(execution or {}),
        "source": dict(source or {}),
        "created_at": stamp(datetime.now(timezone.utc)),
        "updated_at": stamp(datetime.now(timezone.utc)),
        "run_at": aware(parsed_run_at, tz).isoformat(),
    }
    if normalized_recurrence:
        payload["recurrence"] = normalized_recurrence
    _validate_execution(payload["execution"])
    TaskStore(root).insert_task(payload)
    return get_scheduled_task(root, task_id)


def _validate_execution(execution: dict) -> None:
    allowed = {"max_lateness_seconds", "block_on_uncertain"}
    if set(execution) - allowed:
        raise ValueError("Unsupported execution settings")
    value = execution.get("max_lateness_seconds", 86400)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_lateness_seconds must be a positive integer")
    if not isinstance(execution.get("block_on_uncertain", False), bool):
        raise ValueError("block_on_uncertain must be a boolean")


def _record(tasks_dir, payload):
    tz = payload.get("timezone") or system_timezone()
    when = aware(payload.get("next_due") or payload["run_at"], tz).astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    status = payload.get("status", "active")
    state = "running" if payload.get("running") else "completed" if status == "completed" else "failed" if status in {"failed", "needs_review"} else "pending"
    path = Path(tasks_dir) / "tasks.sqlite3"
    return ScheduledTaskRecord(path=path, run_at=when, kind="task", state=state, payload=payload)


def list_active_task_records(tasks_dir):
    return [r for r in list_task_records(tasks_dir) if r.status in {"active", "paused"}]


def list_active_task_views(tasks_dir):
    return [r.to_task_view() for r in list_active_task_records(tasks_dir)]


def list_task_records(tasks_dir, *, include_failed=True, include_archived=False):
    records = [_record(tasks_dir, p) for p in TaskStore(tasks_dir).tasks(include_completed=include_archived)]
    return sorted([r for r in records if (include_failed or r.status in {"active", "paused"})], key=lambda r: (r.run_at, r.task_id))


def list_archived_task_records(tasks_dir):
    return sorted([r for r in list_task_records(tasks_dir, include_archived=True) if r.status == "completed"],
                  key=lambda r: str(r.payload.get("completed_at") or r.payload.get("last_run_at") or ""), reverse=True)


def count_archived_task_records(tasks_dir):
    with TaskStore(tasks_dir).connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM tasks WHERE status='completed'").fetchone()[0]


def get_scheduled_task(tasks_dir, task_id):
    if not str(task_id).strip():
        raise ValueError("task_id is required")
    return _record(tasks_dir, TaskStore(tasks_dir).task(str(task_id)))


def delete_scheduled_task(tasks_dir, task_id):
    record = get_scheduled_task(tasks_dir, task_id)
    TaskStore(tasks_dir).delete_task(task_id)
    return record


def duplicate_archived_task(
    tasks_dir: Path | str,
    task_id: str,
    *,
    run_at: str | datetime,
    recurrence: Any = None,
    title: Optional[str] = None,
    content: Optional[str] = None,
    task_type: Optional[str] = None,
    timezone_name: str | None = None,
    execution: dict | None = None,
) -> ScheduledTaskRecord:
    """Create a fresh task from a completed record while preserving delivery authority."""
    original = get_scheduled_task(tasks_dir, task_id)
    if original.state != TASK_STATE_COMPLETED:
        raise ValueError("only completed archived tasks can be duplicated")
    tz = validate_timezone(timezone_name or original.payload["timezone"])
    try:
        parsed_run_at = datetime.fromisoformat(run_at) if isinstance(run_at, str) else run_at
    except ValueError:
        parsed_run_at = parse_run_at(run_at)
    if aware(parsed_run_at, tz) <= datetime.now(timezone.utc):
        raise ValueError("duplicated task must be scheduled in the future")
    delivery = original.delivery
    raw_target = delivery.get("target")
    target = dict(raw_target) if isinstance(raw_target, dict) else {}
    return enqueue_scheduled_task(
        task_type=task_type if task_type is not None else original.task_type,
        content=content if content is not None else original.content,
        run_at=parsed_run_at,
        tasks_dir=tasks_dir,
        channel=original.delivery_channel,
        target=target,
        user_id=original.delivery_user_id,
        title=title if title is not None else original.title,
        recurrence=recurrence,
        source={"source": "task_duplicate", "source_task_id": original.task_id},
        execution=original.execution if execution is None else execution,
        timezone_name=timezone_name or original.payload["timezone"],
    )


def get_pending_scheduled_task(tasks_dir, task_id):
    record = get_scheduled_task(tasks_dir, task_id)
    if record.status not in {"active", "paused"} or record.state == "running":
        raise ValueError("Task is not editable while running or after completion")
    return record


def retry_scheduled_task(tasks_dir, task_id):
    record = get_scheduled_task(tasks_dir, task_id)
    run_id = (record.payload.get("last_run") or {}).get("run_id")
    if not run_id:
        raise ValueError("No failed execution to retry")
    store = TaskStore(tasks_dir)
    run = store.get_run(task_id, run_id)
    store.act_on_run(task_id, run_id, "deliver" if run.get("result") else "prepare", datetime.now(timezone.utc))
    return get_scheduled_task(tasks_dir, task_id)


def pause_scheduled_task(tasks_dir: Path | str, task_id: str) -> ScheduledTaskRecord:
    """Pause a pending scheduled task so the scheduler skips it."""
    record = get_pending_scheduled_task(tasks_dir, task_id)
    if record.is_paused:
        return record
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = dict(record.payload)
    payload["status"] = TASK_STATUS_PAUSED
    payload["paused_at"] = now.isoformat(sep=" ")
    payload["updated_at"] = now.isoformat(sep=" ")
    TaskStore(tasks_dir).update_task(task_id, payload, record.payload["revision"])
    return get_scheduled_task(tasks_dir, task_id)


def resume_scheduled_task(
    tasks_dir: Path | str,
    task_id: str,
    *,
    now: datetime | None = None,
) -> ScheduledTaskRecord:
    """Resume; the scheduler records and coalesces missed occurrences."""
    record = get_pending_scheduled_task(tasks_dir, task_id)
    current = aware(now or datetime.now(timezone.utc), record.payload["timezone"]).astimezone(ZoneInfo(record.payload["timezone"])).replace(tzinfo=None)
    payload = dict(record.payload)
    next_run_at = record.run_at
    payload["status"] = TASK_STATUS_ACTIVE
    payload.pop("paused_at", None)
    payload["updated_at"] = stamp(aware(current, payload["timezone"]))
    payload["run_at"] = next_run_at.isoformat(sep=" ")
    return _rewrite_pending_task(tasks_dir, record, payload, next_run_at)


def update_scheduled_task(
    tasks_dir: Path | str,
    task_id: str,
    *,
    title: Optional[str] = None,
    content: Optional[str] = None,
    task_type: Optional[str] = None,
    run_at: Optional[str | datetime] = None,
    delay_seconds: Optional[int] = None,
    recurrence: Any = None,
    interval_seconds: Optional[int] = None,
    duration_seconds: Optional[int] = None,
    start_at: Optional[str] = None,
    end_at: Optional[str] = None,
    now: datetime | None = None,
    timezone_name: str | None = None,
    execution: dict | None = None,
) -> ScheduledTaskRecord:
    """Patch mutable fields on a pending scheduled task."""
    record = get_pending_scheduled_task(tasks_dir, task_id)
    if timezone_name is not None:
        validate_timezone(timezone_name)
    current = aware(now or datetime.now(timezone.utc), timezone_name or record.payload["timezone"]).astimezone(ZoneInfo(timezone_name or record.payload["timezone"])).replace(tzinfo=None)
    payload = dict(record.payload)
    task_body = dict(record.task)
    if timezone_name is not None:
        payload["timezone"] = timezone_name
    if execution is not None:
        _validate_execution(execution)
        payload["execution"] = dict(execution)

    if title is not None:
        payload["title"] = str(title).strip()
    if content is not None:
        normalized_content = str(content).strip()
        if not normalized_content:
            raise ValueError("scheduled task content must not be empty")
        task_body["content"] = normalized_content
    if task_type is not None:
        normalized_type = str(task_type).strip().lower()
        if normalized_type not in SUPPORTED_TASK_TYPES:
            raise ValueError(f"task_type must be one of: {', '.join(sorted(SUPPORTED_TASK_TYPES))}")
        task_body["type"] = normalized_type
    payload["task"] = task_body

    schedule_retarget = any(value is not None for value in (run_at, delay_seconds, recurrence))
    interval_patch = any(value is not None for value in (interval_seconds, duration_seconds, start_at, end_at))
    next_run_at = record.run_at
    if schedule_retarget and interval_patch:
        raise ValueError("recurrence/run_at cannot be combined with interval_seconds, duration_seconds, start_at, or end_at")

    if interval_patch:
        if not is_interval_recurrence(record.recurrence) and interval_seconds is None:
            raise ValueError("interval_seconds is required when converting a task to interval recurrence")
        every = interval_seconds
        if every is None:
            every = int(record.recurrence[0].get("every_seconds") or 0)
        if not every:
            raise ValueError("interval_seconds is required for interval tasks")
        existing_end = record.recurrence[0].get("end_at") if record.recurrence else None
        existing_start = record.recurrence[0].get("start_at") if record.recurrence else None
        if duration_seconds is None and end_at is None:
            if not existing_end:
                raise ValueError(
                    "interval tasks require a user-provided duration_seconds or end_at; "
                    "ask the user how long to continue or when to stop before creating"
                )
            resolved_end = _local_datetime(str(existing_end), payload["timezone"])
        elif duration_seconds is not None and end_at is not None:
            raise ValueError("interval recurrence requires exactly one user-provided end_at or duration_seconds")
        elif duration_seconds is not None:
            resolved_end = current + timedelta(seconds=int(duration_seconds))
        else:
            resolved_end = _local_datetime(str(end_at or ""), payload["timezone"])
        if start_at is not None:
            resolved_start = _local_datetime(str(start_at), payload["timezone"])
        elif existing_start:
            resolved_start = _local_datetime(str(existing_start), payload["timezone"])
        else:
            resolved_start = None
        interval_rule: dict[str, Any] = {
            "kind": "interval",
            "every_seconds": int(every),
            "end_at": resolved_end.isoformat(sep=" "),
        }
        if resolved_start is not None:
            interval_rule["start_at"] = resolved_start.isoformat(sep=" ")
        normalized_rule = normalize_recurrence_rules([interval_rule])[0]
        payload["recurrence"] = [normalized_rule]
        schedule_changed = any(value is not None for value in (interval_seconds, duration_seconds, start_at, end_at))
        if schedule_changed:
            if resolved_start is not None:
                aligned = align_interval_next_run(
                    now=current,
                    start_at=resolved_start,
                    every_seconds=int(every),
                    end_at=resolved_end,
                )
                if aligned is None:
                    raise ValueError("end_at must be at or after the next run time")
                next_run_at = aligned
            elif interval_seconds is not None and interval_seconds != int(record.recurrence[0].get("every_seconds") or 0):
                next_run_at = current + timedelta(seconds=int(every))
                if next_run_at > resolved_end:
                    raise ValueError("end_at must be at or after the next run time")
            elif next_run_at > resolved_end:
                raise ValueError("end_at must be at or after the next run time")
    elif schedule_retarget:
        if recurrence is not None:
            resolved_run_at, normalized_recurrence = resolve_scheduled_task_run_at(
                run_at=None,
                delay_seconds=delay_seconds if is_interval_recurrence(recurrence) else None,
                recurrence=recurrence,
                now=current,
                timezone_name=payload["timezone"],
            )
            next_run_at = resolved_run_at
            if normalized_recurrence:
                payload["recurrence"] = normalized_recurrence
            else:
                payload.pop("recurrence", None)
        else:
            resolved_run_at, _normalized = resolve_scheduled_task_run_at(
                run_at=run_at,
                delay_seconds=delay_seconds,
                recurrence=None,
                now=current,
                timezone_name=payload["timezone"],
            )
            next_run_at = resolved_run_at
            payload.pop("recurrence", None)

    payload["updated_at"] = stamp(aware(current, payload["timezone"]))
    payload["run_at"] = next_run_at.isoformat(sep=" ")
    if "status" not in payload:
        payload["status"] = TASK_STATUS_PAUSED if record.is_paused else TASK_STATUS_ACTIVE
    return _rewrite_pending_task(tasks_dir, record, payload, next_run_at)

def _rewrite_pending_task(tasks_dir, record, payload, next_run_at):
    TaskStore(tasks_dir).update_task(record.task_id, payload, record.payload["revision"])
    return get_scheduled_task(tasks_dir, record.task_id)


async def collect_scheduled_turn_events(
    event_stream,
) -> tuple[str, list[dict[str, Any]], str]:
    """Consume a scheduled agent turn event stream and return (final_content, raw_attachments, last_error)."""
    final_content = ""
    raw_attachments: list[dict[str, Any]] = []
    last_error = ""
    try:
        async for event in event_stream:
            event_type = event.get("type")
            if event_type in {"error", "aborted"} and event.get("needs_review"):
                raise DeliveryUncertainError(str(event.get("error") or "scheduled tool outcome is unknown"))
            if event_type == "message_done" and str(event.get("phase") or "final") == "final":
                final_content = str(event.get("content") or "").strip() or final_content
                attachments = event.get("attachments")
                if isinstance(attachments, list):
                    raw_attachments.extend(item for item in attachments if isinstance(item, dict))
            elif event_type == "error" and not final_content:
                from .task_receipts import PreparationError
                raise PreparationError(str(event.get("error") or "Agent processing error.").strip(),
                    retryable=event.get("error_code") in {"network_error", "model_unavailable", "empty_response", "timeout", "capacity"})
    finally:
        close = getattr(event_stream, "aclose", None)
        if callable(close):
            await close()
    return final_content, raw_attachments, last_error


class AsyncTaskScheduler:
    """One local worker: persist first, claim atomically, recover conservatively."""

    def __init__(self, tasks_dir, *, can_handle, execute, deliver,
                 shutdown_timeout_seconds=10.0, poll_interval_seconds=1.0,
                 max_concurrent_dispatches=4, logger_=None, now_provider=None):
        if poll_interval_seconds <= 0 or max_concurrent_dispatches <= 0:
            raise ValueError("poll interval and concurrency must be positive")
        self.tasks_dir = Path(tasks_dir).expanduser().resolve()
        self.store = TaskStore(self.tasks_dir)
        self.can_handle, self.execute, self.deliver = can_handle, execute, deliver
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self.poll_interval_seconds, self.max_concurrent_dispatches = poll_interval_seconds, max_concurrent_dispatches
        self.logger = logger_ or logging.getLogger(__name__)
        self.now_provider = now_provider or (lambda: datetime.now(timezone.utc))
        self._task = None
        self._stop_event, self._wake_event = asyncio.Event(), asyncio.Event()
        self._inflight = set()
        self._inflight_records = {}
        self._orphaned_runs = set()

    def _now(self):
        return aware(self.now_provider(), system_timezone())

    @property
    def is_running(self):
        return self._task is not None and not self._task.done()

    def wake(self):
        self._wake_event.set()

    def channel_tasks(self, channel):
        return {t for t, (r, _) in self._inflight_records.items() if r.delivery_channel == channel and not t.done()}

    def status_snapshot(self):
        records = list_task_records(self.tasks_dir)
        return {"running": self.is_running, "inflight": len(self._inflight),
                "pending": sum(r.status in {"active", "paused"} for r in records),
                "failed": sum(r.status == "failed" or (r.payload.get("last_run_status") == "failed" and not r.payload.get("last_run_resolved")) for r in records),
                "needs_review": sum(bool(r.payload.get("unresolved_runs")) for r in records)}

    async def get_status(self):
        return self.status_snapshot()

    async def start(self):
        if self.is_running:
            return
        self._stop_event.clear()
        self.recover_running_tasks()
        self._task = asyncio.create_task(self.run_forever())

    def recover_running_tasks(self):
        return self.store.recover(self._now())

    async def stop(self):
        self._stop_event.set()
        self.wake()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._inflight:
            _, pending = await asyncio.wait(self._inflight, timeout=self.shutdown_timeout_seconds)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)
        # Ownership is retained by RuntimeHost while any tool writer is alive.
        if not any(not t.done() for t in self._inflight):
            self.store.recover(self._now())

    async def run_forever(self):
        while not self._stop_event.is_set():
            try:
                self._wake_event.clear()
                await self.tick(wait_for_dispatches=False)
                await asyncio.wait_for(self._wake_event.wait(), timeout=self.poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("Task scheduler cycle failed; persisted work remains queued")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass

    async def tick(self, *, wait_for_dispatches=True):
        now = self._now()
        for run_id in list(self._orphaned_runs):
            self.store.recover(now, run_id)
            self._orphaned_runs.discard(run_id)
        self.store.materialize(now)
        started = []
        while len(self._inflight) < self.max_concurrent_dispatches:
            run = self.store.claim(now, lambda payload: self.can_handle(_record(self.tasks_dir, dict(payload, status="active"))))
            if run is None:
                break
            payload = dict(run["snapshot"], status="active", next_due=run["due_at"], scheduled_at=run["due_at"])
            record = _record(self.tasks_dir, payload)
            task = asyncio.create_task(self._run(run, record))
            self._inflight.add(task)
            self._inflight_records[task] = (record, run["id"])
            task.add_done_callback(self._done)
            started.append(task)
        if wait_for_dispatches and started:
            await asyncio.gather(*started, return_exceptions=False)
        return min((aware(p["next_due"]) for p in self.store.tasks() if p.get("next_due")), default=None)

    def _done(self, task):
        self._inflight.discard(task)
        entry = self._inflight_records.pop(task, None)
        if entry and (task.cancelled() or task.exception()):
            self._orphaned_runs.add(entry[1])
            try:
                self.store.recover(self._now(), entry[1])
                self._orphaned_runs.discard(entry[1])
            except Exception:
                self.logger.exception("Task recovery deferred until storage is available")
        self.wake()
        if not task.cancelled() and task.exception():
            self.logger.error("Task worker stopped; persisted state will be recovered", exc_info=task.exception())

    async def _run(self, run, record):
        from .task_receipts import PreparationError, task_run_context
        try:
            with task_run_context(self.store, run, self._now):
                with scheduled_delivery_context(ScheduledDeliveryContext(
                    channel=record.delivery_channel, user_id=record.delivery_user_id, target=record.target,
                    metadata={"source": "scheduled_task", "task_id": record.task_id, "run_id": run["id"],
                              "scheduled_run_id": run["id"], "attempt_id": run["attempt_id"], "scheduled_at": run["due_at"]},
                )):
                    if not run.get("result"):
                        result = await self.execute(record)
                        if not isinstance(result, dict) or not (result.get("content") or result.get("attachments")):
                            raise ValueError("Scheduled task produced no final result")
                        run.update(status="result_ready", phase="deliver", result=result)
                        self.store.save_run(run)
                    run.update(status="delivering", phase="deliver")
                    self.store.save_run(run)
                    with strict_task_delivery():
                        receipt = await self.deliver(record, run["result"], run["id"])
                    if not isinstance(receipt, dict) or not receipt.get("accepted"):
                        raise DeliveryUncertainError("Delivery returned no positive acknowledgement")
                    run["delivery_receipt"] = receipt
            run.update(status="succeeded", phase="completed", error=None, finished_at=stamp(self._now()), attempt_finished_at=stamp(self._now()))
            self.store.save_run(run)
        except asyncio.CancelledError:
            # The persisted executing/delivering boundary is reconciled once the
            # handler has stopped. Do not overwrite accepted per-part receipts.
            raise
        except Exception as exc:
            phase = run["phase"]
            parts = self.store.get_run(record.task_id, run["id"])["deliveries"]
            uncertain = isinstance(exc, DeliveryUncertainError) or (phase == "prepare" and run.get("unsafe_tools_started"))
            if phase == "deliver":
                uncertain = uncertain or any(p["status"] in {"sending", "needs_review"} and p.get("uncertain_on_error", True) for p in parts)
                if not parts and not isinstance(exc, ValueError):
                    uncertain = True
            transient = isinstance(exc, (ConnectionError, TimeoutError)) or isinstance(exc, PreparationError) and exc.retryable
            # A failed upload is known unsent and may be retried. A network send
            # with an unknown acknowledgement always requires review instead.
            transient = transient or phase == "deliver" and bool(parts) and not uncertain and not isinstance(exc, (ValueError, FileNotFoundError))
            retry = transient and not uncertain and run["attempt_count"] < 3
            delay = (30, 120)[min(run["attempt_count"] - 1, 1)]
            retry_at = self._now() + timedelta(seconds=delay)
            retry = retry and retry_at <= aware(run["expires_at"])
            run.update(status="needs_review" if uncertain else "retry_wait" if retry else "failed",
                       failure_stage=phase, error=_safe_error_summary(exc), error_type=type(exc).__name__,
                       retry_at=stamp(retry_at) if retry else None,
                       attempt_finished_at=stamp(self._now()), finished_at=None if retry else stamp(self._now()))
            self.store.save_run(run)
            self.logger.warning("Task execution %s failed in %s: %s", run["id"], phase, run["error"])


def _safe_error_summary(error):
    text = " ".join(str(error).split()) or type(error).__name__
    return re.sub(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)\b\s*[:=]\s*[^\s,;]+", r"\1=[redacted]", text)[:500]
