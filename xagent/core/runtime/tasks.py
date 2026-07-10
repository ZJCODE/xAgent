"""Structured file-backed tasks for scheduled runtime delivery."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional

from .scheduler import (
    FAILED_DIRNAME,
    RUNNING_MARKER,
    TASK_TIMESTAMP_FORMAT,
    _fsync_directory,
    _unique_failed_path,
    calculate_next_recurrence_run_at,
    ensure_scheduler_dirs,
    format_task_timestamp,
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
TASK_PAYLOAD_VERSION = 4
TASK_JSON_SUFFIX = ".json"
TASK_STATE_PENDING = "pending"
TASK_STATE_FAILED = "failed"
TASK_STATE_RUNNING = "running"
DEFAULT_RUNTIME_POLL_INTERVAL_SECONDS = 1.0
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
        if self.state in {TASK_STATE_PENDING, TASK_STATE_RUNNING}:
            return TASK_STATUS_ACTIVE
        return self.state

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
        return {
            "task_id": self.task_id,
            "title": self.title or "Reminder",
            "task_type": self.task_type,
            "content": self.content,
            "next_run_at": self.run_at.isoformat(sep=" "),
            "recurrence": self.recurrence or None,
            "status": self.status,
            "state": self.state,
            "reason": self.reason,
            "channel": self.delivery_channel or "local",
            "user_id": self.delivery_user_id,
            "target": self.target,
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


def list_task_records(tasks_dir: Path | str, *, include_failed: bool = True) -> list[ScheduledTaskRecord]:
    """Return pending scheduled tasks, optionally including failed tasks."""
    root, failed = ensure_scheduler_dirs(tasks_dir)
    records: list[ScheduledTaskRecord] = []

    for path in sorted(root.glob(f"*{TASK_JSON_SUFFIX}"), key=lambda item: item.name):
        record = _record_from_json_file(path, state=TASK_STATE_PENDING)
        if record is not None:
            records.append(record)

    if include_failed and failed.is_dir():
        for path in sorted(failed.iterdir(), key=lambda item: item.name):
            if TASK_JSON_SUFFIX not in path.name:
                continue
            record = _record_from_failed_file(path)
            if record is not None:
                records.append(record)

    return sorted(records, key=lambda item: (item.run_at, item.name))


def delete_task_file(tasks_dir: Path | str, name: str) -> ScheduledTaskRecord:
    """Delete a pending or failed task file by name."""
    path = _resolve_task_file(tasks_dir, name)
    record = _record_from_any_file(path)
    if record is None:
        raise ValueError(f"unsupported task file: {name}")
    path.unlink()
    return record


def delete_scheduled_task(tasks_dir: Path | str, task_id: str) -> ScheduledTaskRecord:
    """Delete a scheduled task by stable task id (searches active and failed)."""
    normalized_task_id = str(task_id or "").strip()
    if not normalized_task_id:
        raise ValueError("task_id is required")
    for record in list_task_records(tasks_dir):
        if record.task_id != normalized_task_id:
            continue
        record.path.unlink()
        return record
    raise FileNotFoundError(f"task not found: {normalized_task_id}")


class AsyncTaskScheduler:
    """Consume structured JSON tasks that the current channel can deliver."""

    def __init__(
        self,
        tasks_dir: Path | str,
        *,
        can_handle: Callable[[ScheduledTaskRecord], bool],
        dispatch: Callable[[ScheduledTaskRecord], Awaitable[None]],
        poll_interval_seconds: float = DEFAULT_RUNTIME_POLL_INTERVAL_SECONDS,
        logger_: Optional[logging.Logger] = None,
        now_provider: Callable[[], datetime] | None = None,
    ):
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self.tasks_dir, self.failed_dir = ensure_scheduler_dirs(tasks_dir)
        self.can_handle = can_handle
        self.dispatch = dispatch
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.logger = logger_ or logging.getLogger(__name__)
        self.now_provider = now_provider or datetime.now
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self.recover_running_tasks()
        self._task = asyncio.create_task(self.run_forever())

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

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

    async def run_forever(self) -> None:
        self.logger.info("structured task scheduler started: tasks=%s", self.tasks_dir)
        while not self._stop_event.is_set():
            try:
                next_run_at = await self.tick()
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._sleep_duration(next_run_at),
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.logger.exception("structured scheduler loop error: %s", exc)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval_seconds)
                except asyncio.TimeoutError:
                    pass
        self.logger.info("structured task scheduler stopped")

    async def tick(self) -> Optional[datetime]:
        now = self.now_provider()
        next_run_at: Optional[datetime] = None
        for record in list_active_task_records(self.tasks_dir):
            if record.kind != TASK_KIND_TASK:
                continue
            if not self.can_handle(record):
                continue
            if record.run_at > now:
                if next_run_at is None or record.run_at < next_run_at:
                    next_run_at = record.run_at
                continue

            claimed_path = self._claim(record.path)
            if claimed_path is None:
                continue
            claimed = _record_from_json_file(claimed_path, state=TASK_STATE_RUNNING) or record
            try:
                await self.dispatch(claimed)
            except Exception as exc:
                self.logger.exception("scheduled task failed -> %s: %s", record.name, exc)
                if not claimed.recurrence:
                    self._quarantine(claimed_path, record.name, "failed")
                    continue
                # Recurring task: log the failure but fall through to
                # _complete_record to reschedule instead of
                # quarantining permanently.
            try:
                self._complete_record(claimed_path, claimed)
            except Exception as exc:
                self.logger.exception("scheduled task completion failed -> %s: %s", record.name, exc)
                self._quarantine(claimed_path, record.name, "error")

        return next_run_at

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

    def _complete_record(self, path: Path, record: ScheduledTaskRecord) -> None:
        recurrence = record.recurrence
        if not recurrence:
            path.unlink(missing_ok=True)
            return
        next_run_at = calculate_next_recurrence_run_at(
            recurrence,
            now=self.now_provider(),
            current_run_at=record.run_at,
        )
        if next_run_at is None:
            path.unlink(missing_ok=True)
            return
        self._reschedule_record(path, record, next_run_at)

    def _reschedule_record(self, path: Path, record: ScheduledTaskRecord, next_run_at: datetime) -> None:
        payload = dict(record.payload)
        payload["run_at"] = next_run_at.isoformat(sep=" ")
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


def _record_from_any_file(path: Path) -> Optional[ScheduledTaskRecord]:
    if TASK_JSON_SUFFIX in path.name:
        return _record_from_failed_file(path) if path.parent.name == FAILED_DIRNAME else _record_from_json_file(path)
    return None


def _record_from_failed_file(path: Path) -> Optional[ScheduledTaskRecord]:
    reason = "failed"
    original_name = path.name
    for candidate_reason in ("timeout", "failed", "error", "invalid", "orphaned"):
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
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("invalid task name")
    root, failed = ensure_scheduler_dirs(tasks_dir)
    candidates = [root / name, failed / name]
    for path in candidates:
        resolved = path.resolve()
        if (resolved.is_relative_to(root) or resolved.is_relative_to(failed)) and resolved.is_file():
            return resolved
    raise FileNotFoundError(f"task not found: {name}")
