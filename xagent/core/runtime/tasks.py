"""Structured file-backed tasks for scheduled runtime delivery."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional

from .scheduler import (
    ARCHIVE_DIRNAME,
    FAILED_DIRNAME,
    RUNNING_MARKER,
    TASK_TIMESTAMP_FORMAT,
    _fsync_directory,
    _unique_failed_path,
    align_interval_next_run,
    align_overdue_interval_run_at,
    calculate_next_recurrence_run_at,
    ensure_scheduler_dirs,
    format_task_timestamp,
    is_interval_recurrence,
    is_interval_window_closed,
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
TASK_PAYLOAD_VERSION = 6
TASK_JSON_SUFFIX = ".json"
TASK_STATE_PENDING = "pending"
TASK_STATE_FAILED = "failed"
TASK_STATE_RUNNING = "running"
TASK_STATE_COMPLETED = "completed"
COMPLETION_REASON_INTERVAL_WINDOW_ENDED = "interval_window_ended"
COMPLETION_REASON_RECURRENCE_EXHAUSTED = "recurrence_exhausted"
COMPLETION_REASON_ONE_SHOT_SUCCEEDED = "one_shot_succeeded"
DEFAULT_RUNTIME_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_MAX_CONCURRENT_TASK_DISPATCHES = 4
SUPPORTED_TASK_TYPES = {TASK_TYPE_MESSAGE, TASK_TYPE_AGENT}
SUPPORTED_LIFECYCLE_STATUSES = {
    TASK_STATUS_ACTIVE,
    TASK_STATUS_PAUSED,
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
}


@dataclass(frozen=True)
class ScheduledDeliveryContext:
    """Current conversation target used by scheduling tools."""

    channel: str
    user_id: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScheduledTaskRecord:
    """A structured view of a task file."""

    path: Path
    run_at: datetime
    kind: str
    state: str = TASK_STATE_PENDING
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    @property
    def name(self) -> str:
        return self.path.name

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
        return str(self.task.get("content") or "")

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
        if self.state == TASK_STATE_FAILED:
            return TASK_STATE_FAILED
        if self.state == TASK_STATE_COMPLETED:
            return TASK_STATE_COMPLETED
        raw = str(self.payload.get("status") or "").strip().lower() if isinstance(self.payload, dict) else ""
        if raw == TASK_STATUS_PAUSED:
            return TASK_STATUS_PAUSED
        if self.state in {TASK_STATE_PENDING, TASK_STATE_RUNNING}:
            return TASK_STATUS_ACTIVE
        return raw or TASK_STATUS_ACTIVE

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
        is_terminal = self.status in {TASK_STATUS_COMPLETED, TASK_STATUS_FAILED}
        return {
            "task_id": self.task_id,
            "title": self.title or "Reminder",
            "task_type": self.task_type,
            "content": self.content,
            "next_run_at": None if is_terminal else self.run_at.isoformat(sep=" "),
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


def normalize_task_recurrence(recurrence: Any) -> list[dict[str, Any]]:
    """Normalize optional recurrence rules."""
    return normalize_recurrence_rules(recurrence)


def resolve_scheduled_task_run_at(
    *,
    run_at: Optional[str | datetime] = None,
    delay_seconds: Optional[int] = None,
    recurrence: Any = None,
    now: datetime | None = None,
) -> tuple[datetime, list[dict[str, Any]]]:
    """Resolve creation-time schedule parameters into a concrete next run datetime."""
    current = (now or datetime.now()).replace(microsecond=0)
    normalized_recurrence = materialize_interval_recurrence_rules(recurrence, now=current)
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
        return resolve_recurrence_run_at(normalized_recurrence, now=current), normalized_recurrence

    if delay_seconds is None and run_at is None:
        raise ValueError("Provide either run_at or delay_seconds.")
    if delay_seconds is not None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be zero or positive.")
        return current + timedelta(seconds=delay_seconds), []
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
) -> ScheduledTaskRecord:
    """Atomically enqueue a scheduled task as JSON."""
    normalized_type = str(task_type or "").strip().lower()
    if normalized_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(f"task_type must be one of: {', '.join(sorted(SUPPORTED_TASK_TYPES))}")
    normalized_content = content.strip()
    if not normalized_content:
        raise ValueError("scheduled task content must not be empty")
    normalized_recurrence = materialize_interval_recurrence_rules(
        recurrence,
        now=datetime.now().replace(microsecond=0),
    )
    parsed_run_at = parse_run_at(run_at)
    root, _failed = ensure_scheduler_dirs(tasks_dir)
    task_id = uuid.uuid4().hex
    payload = {
        "version": TASK_PAYLOAD_VERSION,
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
        "created_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "updated_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "run_at": parsed_run_at.isoformat(sep=" "),
    }
    if normalized_recurrence:
        payload["recurrence"] = normalized_recurrence
    path = _enqueue_json_payload(payload, parsed_run_at, root, task_id=task_id)
    return ScheduledTaskRecord(path=path, run_at=parsed_run_at, kind=TASK_KIND_TASK, payload=payload)


def list_active_task_records(tasks_dir: Path | str) -> list[ScheduledTaskRecord]:
    """Return active scheduled tasks only."""
    return list_task_records(tasks_dir, include_failed=False)


def list_active_task_views(tasks_dir: Path | str) -> list[dict[str, Any]]:
    """Return active scheduled tasks in the user-facing tool/API shape."""
    return [record.to_task_view() for record in list_active_task_records(tasks_dir)]


def list_task_records(
    tasks_dir: Path | str,
    *,
    include_failed: bool = True,
    include_archived: bool = False,
    include_running: bool = False,
) -> list[ScheduledTaskRecord]:
    """Return current tasks, optionally including failed, archived, and running records.

    ``include_running`` is opt-in because the scheduler's ``tick()`` and the task
    mutation helpers must not see files claimed for dispatch (they would try to
    re-claim or operate on a renamed running file). Display callers (the HTTP API,
    CLI listings) pass ``True`` so an interval task remains visible while it is
    dispatching instead of blinking out of the list.
    """
    root, failed = ensure_scheduler_dirs(tasks_dir)
    records: list[ScheduledTaskRecord] = []

    for path in sorted(root.glob(f"*{TASK_JSON_SUFFIX}"), key=lambda item: item.name):
        record = _record_from_json_file(path, state=TASK_STATE_PENDING)
        if record is not None:
            records.append(record)

    if include_running:
        for path in sorted(root.glob(f"*{TASK_JSON_SUFFIX}{RUNNING_MARKER}*"), key=lambda item: item.name):
            record = _record_from_json_file(path, state=TASK_STATE_RUNNING)
            if record is not None:
                records.append(record)

    if include_failed and failed.is_dir():
        for path in sorted(failed.iterdir(), key=lambda item: item.name):
            if TASK_JSON_SUFFIX not in path.name:
                continue
            record = _record_from_failed_file(path)
            if record is not None:
                records.append(record)

    if include_archived:
        records.extend(list_archived_task_records(tasks_dir))

    return sorted(records, key=lambda item: (item.run_at, item.name))


def list_archived_task_records(tasks_dir: Path | str) -> list[ScheduledTaskRecord]:
    """Return completed task records, newest completion first."""
    root, _failed = ensure_scheduler_dirs(tasks_dir)
    archive = root / ARCHIVE_DIRNAME
    records: list[ScheduledTaskRecord] = []
    if archive.is_dir():
        for path in archive.rglob(f"*{TASK_JSON_SUFFIX}"):
            record = _record_from_json_file(path, state=TASK_STATE_COMPLETED)
            if record is not None:
                records.append(record)
    return sorted(
        records,
        key=lambda item: (str(item.payload.get("completed_at") or ""), item.name),
        reverse=True,
    )


def count_archived_task_records(tasks_dir: Path | str) -> int:
    """Count archived task files without parsing their payloads."""
    root, _failed = ensure_scheduler_dirs(tasks_dir)
    archive = root / ARCHIVE_DIRNAME
    if not archive.is_dir():
        return 0
    return sum(1 for path in archive.rglob(f"*{TASK_JSON_SUFFIX}") if path.is_file())


def get_scheduled_task(tasks_dir: Path | str, task_id: str) -> ScheduledTaskRecord:
    """Return a task by id across pending, failed, and archived storage."""
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        raise ValueError("task_id is required")
    for record in list_task_records(tasks_dir, include_archived=True):
        if record.task_id == normalized_task_id:
            return record
    raise FileNotFoundError(f"task not found: {normalized_task_id}")


def delete_task_file(tasks_dir: Path | str, name: str) -> ScheduledTaskRecord:
    """Delete a pending, failed, or archived task file by name."""
    path = _resolve_task_file(tasks_dir, name)
    record = _record_from_any_file(path)
    if record is None:
        raise ValueError(f"unsupported task file: {name}")
    path.unlink()
    return record


def delete_scheduled_task(tasks_dir: Path | str, task_id: str) -> ScheduledTaskRecord:
    """Permanently delete a task by stable id from any lifecycle location."""
    record = get_scheduled_task(tasks_dir, task_id)
    record.path.unlink()
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
) -> ScheduledTaskRecord:
    """Create a fresh task from a completed record while preserving delivery authority."""
    original = get_scheduled_task(tasks_dir, task_id)
    if original.state != TASK_STATE_COMPLETED:
        raise ValueError("only completed archived tasks can be duplicated")
    parsed_run_at = parse_run_at(run_at)
    if parsed_run_at <= datetime.now().replace(microsecond=0):
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
        execution=original.execution,
    )


def get_pending_scheduled_task(tasks_dir: Path | str, task_id: str) -> ScheduledTaskRecord:
    """Return a pending (non-failed) scheduled task by id."""
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        raise ValueError("task_id is required")
    for record in list_task_records(tasks_dir, include_failed=False):
        if record.task_id == normalized_task_id:
            return record
    raise FileNotFoundError(f"task not found: {normalized_task_id}")


def pause_scheduled_task(tasks_dir: Path | str, task_id: str) -> ScheduledTaskRecord:
    """Pause a pending scheduled task so the scheduler skips it."""
    record = get_pending_scheduled_task(tasks_dir, task_id)
    if record.is_paused:
        return record
    now = datetime.now().replace(microsecond=0)
    payload = dict(record.payload)
    payload["version"] = TASK_PAYLOAD_VERSION
    payload["status"] = TASK_STATUS_PAUSED
    payload["paused_at"] = now.isoformat(sep=" ")
    payload["updated_at"] = now.isoformat(sep=" ")
    _replace_json_payload(record.path, payload)
    return ScheduledTaskRecord(
        path=record.path,
        run_at=record.run_at,
        kind=record.kind,
        state=record.state,
        payload=payload,
        reason=record.reason,
    )


def resume_scheduled_task(
    tasks_dir: Path | str,
    task_id: str,
    *,
    now: datetime | None = None,
) -> ScheduledTaskRecord:
    """Resume a paused task and advance overdue run_at when needed."""
    record = get_pending_scheduled_task(tasks_dir, task_id)
    current = (now or datetime.now()).replace(microsecond=0)
    payload = dict(record.payload)
    next_run_at = record.run_at
    if record.is_paused or next_run_at <= current:
        next_run_at = _next_run_at_after_resume(record, now=current)
    payload["version"] = TASK_PAYLOAD_VERSION
    payload["status"] = TASK_STATUS_ACTIVE
    payload.pop("paused_at", None)
    payload["updated_at"] = current.isoformat(sep=" ")
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
) -> ScheduledTaskRecord:
    """Patch mutable fields on a pending scheduled task."""
    record = get_pending_scheduled_task(tasks_dir, task_id)
    current = (now or datetime.now()).replace(microsecond=0)
    payload = dict(record.payload)
    task_body = dict(record.task)

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
            resolved_end = parse_run_at(str(existing_end))
        elif duration_seconds is not None and end_at is not None:
            raise ValueError("interval recurrence requires exactly one user-provided end_at or duration_seconds")
        elif duration_seconds is not None:
            resolved_end = current + timedelta(seconds=int(duration_seconds))
        else:
            resolved_end = parse_run_at(str(end_at or ""))
        if start_at is not None:
            resolved_start = parse_run_at(str(start_at))
        elif existing_start:
            resolved_start = parse_run_at(str(existing_start))
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
            )
            next_run_at = resolved_run_at
            payload.pop("recurrence", None)

    payload["version"] = TASK_PAYLOAD_VERSION
    payload["updated_at"] = current.isoformat(sep=" ")
    payload["run_at"] = next_run_at.isoformat(sep=" ")
    if "status" not in payload:
        payload["status"] = TASK_STATUS_PAUSED if record.is_paused else TASK_STATUS_ACTIVE
    return _rewrite_pending_task(tasks_dir, record, payload, next_run_at)

def _next_run_at_after_resume(record: ScheduledTaskRecord, *, now: datetime) -> datetime:
    if record.run_at > now:
        return record.run_at
    recurrence = record.recurrence
    if not recurrence:
        return now
    if is_interval_recurrence(recurrence):
        rule = recurrence[0]
        start_at_text = str(rule.get("start_at") or "").strip()
        if start_at_text:
            aligned = align_interval_next_run(
                now=now,
                start_at=parse_run_at(start_at_text),
                every_seconds=int(rule.get("every_seconds") or 0),
                end_at=parse_run_at(str(rule.get("end_at") or "")),
            )
            if aligned is None:
                raise ValueError("interval window has already ended")
            return aligned
        return resolve_interval_first_run_at(rule, now=now)
    return resolve_recurrence_run_at(recurrence, now=now)


def _rewrite_pending_task(
    tasks_dir: Path | str,
    record: ScheduledTaskRecord,
    payload: dict[str, Any],
    next_run_at: datetime,
) -> ScheduledTaskRecord:
    root, _failed = ensure_scheduler_dirs(tasks_dir)
    if next_run_at == record.run_at and record.path.parent == root:
        _replace_json_payload(record.path, payload)
        return ScheduledTaskRecord(
            path=record.path,
            run_at=next_run_at,
            kind=record.kind,
            state=record.state,
            payload=payload,
            reason=record.reason,
        )
    _replace_json_payload(record.path, payload)
    new_path = _move_running_task(record.path, root, next_run_at, task_id=record.task_id)
    return ScheduledTaskRecord(
        path=new_path,
        run_at=next_run_at,
        kind=record.kind,
        state=record.state,
        payload=payload,
        reason=record.reason,
    )


class AsyncTaskScheduler:
    """Consume structured JSON tasks that the current channel can deliver."""

    def __init__(
        self,
        tasks_dir: Path | str,
        *,
        can_handle: Callable[[ScheduledTaskRecord], bool],
        dispatch: Callable[[ScheduledTaskRecord], Awaitable[None]],
        poll_interval_seconds: float = DEFAULT_RUNTIME_POLL_INTERVAL_SECONDS,
        max_concurrent_dispatches: int = DEFAULT_MAX_CONCURRENT_TASK_DISPATCHES,
        logger_: Optional[logging.Logger] = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if max_concurrent_dispatches <= 0:
            raise ValueError("max_concurrent_dispatches must be positive")
        self.tasks_dir, self.failed_dir = ensure_scheduler_dirs(tasks_dir)
        self.can_handle = can_handle
        self.dispatch = dispatch
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.max_concurrent_dispatches = int(max_concurrent_dispatches)
        self.logger = logger_ or logging.getLogger(__name__)
        self.now_provider = now_provider or datetime.now
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._inflight: set[asyncio.Task[None]] = set()
        self._dispatch_semaphore = asyncio.Semaphore(self.max_concurrent_dispatches)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self.recover_running_tasks()
        self.expire_closed_interval_tasks()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)
        self._inflight.clear()

    def recover_running_tasks(self) -> int:
        recovered = 0
        for path in sorted(self.tasks_dir.glob(f"*{TASK_JSON_SUFFIX}{RUNNING_MARKER}*"), key=lambda item: item.name):
            original_name = path.name.split(RUNNING_MARKER, 1)[0]
            if _parse_task_time_from_json_name(original_name) is None:
                self._quarantine(path, original_name, "invalid")
                continue
            destination = self.tasks_dir / original_name
            if destination.exists():
                self._quarantine(path, original_name, "orphaned")
                continue
            try:
                path.rename(destination)
            except OSError as exc:
                self.logger.error("failed to recover running task %s: %s", path.name, exc)
                continue
            recovered += 1
        return recovered

    def expire_closed_interval_tasks(self) -> int:
        """Archive interval tasks whose end_at is strictly in the past.

        Ignores ``can_handle``: window close is a lifecycle transition, not delivery.
        Skips already-claimed running files to avoid racing in-flight dispatches.
        """
        now = self.now_provider().replace(microsecond=0)
        expired = 0
        for record in list_active_task_records(self.tasks_dir):
            if record.kind != TASK_KIND_TASK:
                continue
            if not is_interval_window_closed(record.recurrence, now=now):
                continue
            claimed_path = self._claim(record.path)
            if claimed_path is None:
                continue
            claimed = _record_from_json_file(claimed_path, state=TASK_STATE_RUNNING) or record
            try:
                self._archive_record(
                    claimed_path,
                    claimed,
                    now=now,
                    reason=COMPLETION_REASON_INTERVAL_WINDOW_ENDED,
                )
                expired += 1
            except Exception as exc:
                self.logger.exception("failed to expire closed interval task %s: %s", record.name, exc)
                self._fail_record(claimed_path, claimed, "expire_error", exc)
        return expired

    async def run_forever(self) -> None:
        self.logger.info("structured task scheduler started: tasks=%s", self.tasks_dir)
        while not self._stop_event.is_set():
            try:
                next_run_at = await self.tick(wait_for_dispatches=False)
                await self._wait_for_next_cycle(next_run_at)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.exception("structured scheduler loop error: %s", exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        self.logger.info("structured task scheduler stopped")

    async def _wait_for_next_cycle(self, next_run_at: Optional[datetime]) -> None:
        timeout = self._sleep_duration(next_run_at)
        self._wake_event.clear()
        stop_wait = asyncio.create_task(self._stop_event.wait())
        wake_wait = asyncio.create_task(self._wake_event.wait())
        try:
            done, pending = await asyncio.wait(
                {stop_wait, wake_wait},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            _ = done
        except asyncio.CancelledError:
            stop_wait.cancel()
            wake_wait.cancel()
            await asyncio.gather(stop_wait, wake_wait, return_exceptions=True)
            raise

    async def tick(self, *, wait_for_dispatches: bool = True) -> Optional[datetime]:
        self.expire_closed_interval_tasks()
        now = self.now_provider()
        next_run_at: Optional[datetime] = None
        started: list[asyncio.Task[None]] = []

        for record in list_active_task_records(self.tasks_dir):
            if record.kind != TASK_KIND_TASK:
                continue
            if record.is_paused:
                continue
            if not self.can_handle(record):
                continue
            record = self._skip_missed_interval_ticks(record, now=now)
            if record is None:
                continue
            if record.run_at > now:
                if next_run_at is None or record.run_at < next_run_at:
                    next_run_at = record.run_at
                continue
            if len(self._inflight) >= self.max_concurrent_dispatches:
                # More due work remains; wake ASAP once a slot frees.
                if next_run_at is None or now < next_run_at:
                    next_run_at = now
                break

            claimed_path = self._claim(record.path)
            if claimed_path is None:
                continue
            claimed = _record_from_json_file(claimed_path, state=TASK_STATE_RUNNING) or record
            task = asyncio.create_task(self._run_claimed(claimed_path, claimed))
            self._inflight.add(task)
            task.add_done_callback(self._on_dispatch_done)
            started.append(task)

        if wait_for_dispatches and started:
            await asyncio.gather(*started, return_exceptions=True)
        return next_run_at

    def _skip_missed_interval_ticks(
        self,
        record: ScheduledTaskRecord,
        *,
        now: datetime,
    ) -> ScheduledTaskRecord | None:
        """Realign overdue interval tasks without catching up missed ticks.

        Returns the (possibly rewritten) pending record, or ``None`` when the
        remaining window has no further runnable tick and the task was archived.
        """
        if not is_interval_recurrence(record.recurrence):
            return record
        if record.run_at > now:
            return record
        aligned = align_overdue_interval_run_at(record.run_at, record.recurrence[0], now=now)
        if aligned is None:
            claimed_path = self._claim(record.path)
            if claimed_path is None:
                return None
            claimed = _record_from_json_file(claimed_path, state=TASK_STATE_RUNNING) or record
            try:
                self._archive_record(
                    claimed_path,
                    claimed,
                    now=now,
                    reason=COMPLETION_REASON_INTERVAL_WINDOW_ENDED,
                )
            except Exception as exc:
                self.logger.exception("failed to archive exhausted interval task %s: %s", record.name, exc)
                self._fail_record(claimed_path, claimed, "expire_error", exc)
            return None
        if aligned == record.run_at:
            return record
        payload = dict(record.payload)
        payload["run_at"] = aligned.isoformat(sep=" ")
        payload["updated_at"] = now.isoformat(sep=" ")
        return _rewrite_pending_task(self.tasks_dir, record, payload, aligned)

    def _on_dispatch_done(self, task: asyncio.Task[None]) -> None:
        self._inflight.discard(task)
        self._wake_event.set()

    async def _run_claimed(self, claimed_path: Path, claimed: ScheduledTaskRecord) -> None:
        async with self._dispatch_semaphore:
            dispatch_error: Exception | None = None
            try:
                await self.dispatch(claimed)
            except Exception as exc:
                dispatch_error = exc
                self.logger.exception("scheduled task failed -> %s: %s", claimed.name, exc)
                if not claimed.recurrence:
                    self._fail_record(claimed_path, claimed, "failed", exc)
                    return
            try:
                self._complete_record(claimed_path, claimed, dispatch_error=dispatch_error)
            except Exception as exc:
                self.logger.exception("scheduled task completion failed -> %s: %s", claimed.name, exc)
                self._fail_record(claimed_path, claimed, "completion_error", exc)

    def _sleep_duration(self, next_run_at: Optional[datetime]) -> float:
        if next_run_at is None:
            return self.poll_interval_seconds
        delay = (next_run_at - self.now_provider()).total_seconds()
        if delay <= 0:
            return 0.0
        return min(delay, self.poll_interval_seconds)

    def _claim(self, path: Path) -> Optional[Path]:
        for _attempt in range(8):
            claimed_path = path.with_name(f"{path.name}{RUNNING_MARKER}{uuid.uuid4().hex[:8]}")
            if claimed_path.exists():
                continue
            try:
                path.rename(claimed_path)
                return claimed_path
            except FileNotFoundError:
                return None
            except OSError as exc:
                self.logger.error("failed to claim structured task %s: %s", path.name, exc)
                return None
        self.logger.error("failed to claim structured task %s: could not reserve running name", path.name)
        return None

    def _quarantine(self, path: Path, original_name: str, reason: str) -> None:
        self.failed_dir.mkdir(parents=True, exist_ok=True)
        destination = _unique_failed_path(self.failed_dir, original_name, reason)
        try:
            path.rename(destination)
        except FileNotFoundError:
            return
        except OSError as exc:
            self.logger.error("failed to quarantine structured task %s: %s", original_name, exc)

    def _fail_record(
        self,
        path: Path,
        record: ScheduledTaskRecord,
        reason: str,
        error: Exception,
    ) -> None:
        now = self.now_provider().replace(microsecond=0)
        payload = dict(record.payload)
        payload.update(
            {
                "version": TASK_PAYLOAD_VERSION,
                "status": TASK_STATUS_FAILED,
                "failed_at": now.isoformat(sep=" "),
                "last_run_at": now.isoformat(sep=" "),
                "last_run_status": "failed",
                "last_error": _safe_error_summary(error),
                "updated_at": now.isoformat(sep=" "),
            }
        )
        try:
            _replace_json_payload(path, payload)
        except OSError as exc:
            self.logger.error("failed to persist task failure metadata for %s: %s", record.name, exc)
        self._quarantine(path, _original_task_name(path), reason)

    def _complete_record(
        self,
        path: Path,
        record: ScheduledTaskRecord,
        *,
        dispatch_error: Exception | None = None,
    ) -> None:
        recurrence = record.recurrence
        now = self.now_provider().replace(microsecond=0)
        if not recurrence:
            self._archive_record(path, record, now=now, reason=COMPLETION_REASON_ONE_SHOT_SUCCEEDED)
            return
        if is_interval_window_closed(recurrence, now=now):
            if dispatch_error is not None:
                self._fail_record(path, record, "failed", dispatch_error)
            else:
                self._archive_record(path, record, now=now, reason=COMPLETION_REASON_INTERVAL_WINDOW_ENDED)
            return
        next_run_at = calculate_next_recurrence_run_at(
            recurrence,
            now=now,
            current_run_at=record.run_at,
        )
        if next_run_at is None:
            if dispatch_error is not None:
                self._fail_record(path, record, "failed", dispatch_error)
            else:
                self._archive_record(path, record, now=now, reason=COMPLETION_REASON_RECURRENCE_EXHAUSTED)
            return
        self._reschedule_record(path, record, next_run_at, now=now, dispatch_error=dispatch_error)

    def _archive_record(
        self,
        path: Path,
        record: ScheduledTaskRecord,
        *,
        now: datetime,
        reason: str,
    ) -> None:
        payload = dict(record.payload)
        payload.update(
            {
                "version": TASK_PAYLOAD_VERSION,
                "status": TASK_STATUS_COMPLETED,
                "completed_at": now.isoformat(sep=" "),
                "last_run_at": now.isoformat(sep=" "),
                "last_run_status": "succeeded",
                "completion_reason": reason,
                "updated_at": now.isoformat(sep=" "),
            }
        )
        payload.pop("last_error", None)
        _replace_json_payload(path, payload)
        _move_task_to_archive(path, self.tasks_dir, now, task_id=record.task_id)

    def _reschedule_record(
        self,
        path: Path,
        record: ScheduledTaskRecord,
        next_run_at: datetime,
        *,
        now: datetime,
        dispatch_error: Exception | None,
    ) -> None:
        payload = dict(record.payload)
        payload["run_at"] = next_run_at.isoformat(sep=" ")
        payload["updated_at"] = now.isoformat(sep=" ")
        payload["last_run_at"] = now.isoformat(sep=" ")
        if dispatch_error is None:
            payload["last_run_status"] = "succeeded"
            payload.pop("last_error", None)
        else:
            payload["last_run_status"] = "failed"
            payload["last_error"] = _safe_error_summary(dispatch_error)
        _replace_json_payload(path, payload)
        _move_running_task(path, self.tasks_dir, next_run_at, task_id=record.task_id)


def _enqueue_json_payload(payload: dict[str, Any], run_at: datetime, root: Path, *, task_id: str) -> Path:
    stamp = format_task_timestamp(run_at)
    temp_path = root / f".{stamp}-{uuid.uuid4().hex}.tmp"

    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    try:
        for final_path in _task_file_candidates(root, run_at, task_id=task_id):
            try:
                os.link(temp_path, final_path)
            except FileExistsError:
                continue
            _fsync_directory(root)
            return final_path
        raise FileExistsError(f"could not reserve a unique task filename for {stamp}")
    finally:
        temp_path.unlink(missing_ok=True)


def _replace_json_payload(path: Path, payload: dict[str, Any]) -> None:
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)
    _fsync_directory(path.parent)


def _task_file_candidates(root: Path, run_at: datetime, *, task_id: str) -> Iterator[Path]:
    stamp = format_task_timestamp(run_at)
    yield root / f"{stamp}-{task_id[:8]}{TASK_JSON_SUFFIX}"
    for _ in range(32):
        yield root / f"{stamp}-{uuid.uuid4().hex[:8]}{TASK_JSON_SUFFIX}"


def _move_running_task(path: Path, root: Path, run_at: datetime, *, task_id: str) -> Path:
    for candidate in _task_file_candidates(root, run_at, task_id=task_id):
        try:
            os.link(path, candidate)
        except FileExistsError:
            continue
        _fsync_directory(root)
        path.unlink(missing_ok=True)
        return candidate
    raise FileExistsError(f"could not reserve a unique task filename for {format_task_timestamp(run_at)}")


def _move_task_to_archive(path: Path, root: Path, completed_at: datetime, *, task_id: str) -> Path:
    archive_dir = root / ARCHIVE_DIRNAME / completed_at.strftime("%Y-%m")
    archive_dir.mkdir(parents=True, exist_ok=True)
    for candidate in _task_file_candidates(archive_dir, completed_at, task_id=task_id):
        try:
            os.link(path, candidate)
        except FileExistsError:
            continue
        _fsync_directory(archive_dir)
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
        return candidate
    raise FileExistsError(f"could not reserve archive filename for {format_task_timestamp(completed_at)}")


def _original_task_name(path: Path) -> str:
    return path.name.split(RUNNING_MARKER, 1)[0]


def _safe_error_summary(error: Exception) -> str:
    text = " ".join(str(error).split()).strip()
    if not text:
        text = error.__class__.__name__
    text = re.sub(
        r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password)\b\s*[:=]\s*[^\s,;]+",
        r"\1=[redacted]",
        text,
    )
    return text[:500]


def _record_from_any_file(path: Path) -> Optional[ScheduledTaskRecord]:
    if TASK_JSON_SUFFIX in path.name:
        if path.parent.name == FAILED_DIRNAME:
            return _record_from_failed_file(path)
        if ARCHIVE_DIRNAME in path.parts:
            return _record_from_json_file(path, state=TASK_STATE_COMPLETED)
        return _record_from_json_file(path)
    return None


def _record_from_failed_file(path: Path) -> Optional[ScheduledTaskRecord]:
    reason = "failed"
    original_name = path.name
    for candidate_reason in ("completion_error", "timeout", "failed", "error", "invalid", "orphaned"):
        marker = f".{candidate_reason}"
        if marker in path.name:
            reason = candidate_reason
            original_name = path.name.split(marker, 1)[0]
            break
    if not original_name.endswith(TASK_JSON_SUFFIX):
        return None
    record = _record_from_json_file(path, state=TASK_STATE_FAILED, original_name=original_name, reason=reason)
    return record


def _record_from_json_file(
    path: Path,
    *,
    state: str = TASK_STATE_PENDING,
    original_name: Optional[str] = None,
    reason: str = "",
) -> Optional[ScheduledTaskRecord]:
    run_at = _parse_task_time_from_json_name(original_name or path.name)
    if run_at is None:
        return None
    try:
        raw_payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw_payload = {}
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    payload_run_at = payload.get("run_at")
    if payload_run_at:
        try:
            run_at = parse_run_at(str(payload_run_at))
        except ValueError:
            pass
    kind = str(payload.get("kind") or "")
    return ScheduledTaskRecord(path=path, run_at=run_at, kind=kind, state=state, payload=payload, reason=reason)


def _parse_task_time_from_json_name(name: str) -> Optional[datetime]:
    base_name = name.split(RUNNING_MARKER, 1)[0]
    if not base_name.endswith(TASK_JSON_SUFFIX):
        return None
    stamp = base_name[:len(datetime.now().strftime(TASK_TIMESTAMP_FORMAT))]
    try:
        return datetime.strptime(stamp, TASK_TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _resolve_task_file(tasks_dir: Path | str, name: str) -> Path:
    if not name or any(char in name for char in ("/", "\\", "*", "?", "[", "]")) or name in {".", ".."}:
        raise ValueError("invalid task name")
    root, failed = ensure_scheduler_dirs(tasks_dir)
    archive = root / ARCHIVE_DIRNAME
    candidates = [root / name, failed / name]
    if archive.is_dir():
        candidates.extend(archive.rglob(name))
    for path in candidates:
        resolved = path.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    raise FileNotFoundError(f"task not found: {name}")
