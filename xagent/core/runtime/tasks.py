"""Structured file-backed tasks for runtime message delivery."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Optional

from .scheduler import (
    FAILED_DIRNAME,
    RUNNING_MARKER,
    TASK_TIMESTAMP_FORMAT,
    _fsync_directory,
    _unique_failed_path,
    ensure_scheduler_dirs,
    format_task_timestamp,
    list_scheduled_tasks,
    parse_run_at,
    parse_task_filename,
)


TASK_KIND_MESSAGE = "message"
TASK_KIND_COMMAND = "command"
TASK_PAYLOAD_VERSION = 1
TASK_JSON_SUFFIX = ".json"
TASK_STATE_PENDING = "pending"
TASK_STATE_FAILED = "failed"
TASK_STATE_RUNNING = "running"
DEFAULT_RUNTIME_POLL_INTERVAL_SECONDS = 1.0


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
    def message(self) -> str:
        return str(self.payload.get("message") or "") if isinstance(self.payload, dict) else ""

    @property
    def target(self) -> dict[str, Any]:
        target = self.payload.get("target") if isinstance(self.payload, dict) else None
        return dict(target) if isinstance(target, dict) else {}

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


def enqueue_message_task(
    *,
    message: str,
    run_at: str | datetime,
    tasks_dir: Path | str,
    target: dict[str, Any],
    user_id: str = "",
    title: str = "",
    source: Optional[dict[str, Any]] = None,
) -> ScheduledTaskRecord:
    """Atomically enqueue a scheduled message task as JSON."""
    content = message.strip()
    if not content:
        raise ValueError("scheduled message must not be empty")
    parsed_run_at = parse_run_at(run_at)
    root, _failed = ensure_scheduler_dirs(tasks_dir)
    task_id = uuid.uuid4().hex
    payload = {
        "version": TASK_PAYLOAD_VERSION,
        "id": task_id,
        "kind": TASK_KIND_MESSAGE,
        "title": title.strip() if title else "",
        "message": content,
        "user_id": user_id,
        "target": dict(target or {}),
        "source": dict(source or {}),
        "created_at": datetime.now().replace(microsecond=0).isoformat(sep=" "),
        "run_at": parsed_run_at.isoformat(sep=" "),
    }
    path = _enqueue_json_payload(payload, parsed_run_at, root, task_id=task_id)
    return ScheduledTaskRecord(path=path, run_at=parsed_run_at, kind=TASK_KIND_MESSAGE, payload=payload)


def list_task_records(tasks_dir: Path | str, *, include_failed: bool = True) -> list[ScheduledTaskRecord]:
    """Return pending structured and shell tasks, optionally including failed tasks."""
    root, failed = ensure_scheduler_dirs(tasks_dir)
    records: list[ScheduledTaskRecord] = []

    for shell_task in list_scheduled_tasks(root, include_commands=True):
        records.append(ScheduledTaskRecord(
            path=shell_task.path,
            run_at=shell_task.run_at,
            kind=TASK_KIND_COMMAND,
            payload={"command": shell_task.command},
        ))

    for path in sorted(root.glob(f"*{TASK_JSON_SUFFIX}"), key=lambda item: item.name):
        record = _record_from_json_file(path, state=TASK_STATE_PENDING)
        if record is not None:
            records.append(record)

    if include_failed and failed.is_dir():
        for path in sorted(failed.iterdir(), key=lambda item: item.name):
            if TASK_JSON_SUFFIX not in path.name and not path.name.endswith(".sh.failed"):
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
        for record in list_task_records(self.tasks_dir, include_failed=False):
            if record.kind != TASK_KIND_MESSAGE:
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
                self.logger.exception("scheduled message task failed -> %s: %s", record.name, exc)
                self._quarantine(claimed_path, record.name, "failed")
                continue
            claimed_path.unlink(missing_ok=True)

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


def _enqueue_json_payload(payload: dict[str, Any], run_at: datetime, root: Path, *, task_id: str) -> Path:
    stamp = format_task_timestamp(run_at)
    temp_path = root / f".{stamp}-{uuid.uuid4().hex}.tmp"
    candidates = [f"{stamp}-{task_id[:8]}{TASK_JSON_SUFFIX}"]
    candidates.extend(f"{stamp}-{uuid.uuid4().hex[:8]}{TASK_JSON_SUFFIX}" for _ in range(32))

    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())

    try:
        for candidate in candidates:
            final_path = root / candidate
            try:
                os.link(temp_path, final_path)
            except FileExistsError:
                continue
            _fsync_directory(root)
            return final_path
        raise FileExistsError(f"could not reserve a unique task filename for {stamp}")
    finally:
        temp_path.unlink(missing_ok=True)


def _record_from_any_file(path: Path) -> Optional[ScheduledTaskRecord]:
    if TASK_JSON_SUFFIX in path.name:
        return _record_from_failed_file(path) if path.parent.name == FAILED_DIRNAME else _record_from_json_file(path)
    if path.suffix == ".sh" or ".sh." in path.name:
        return _record_from_shell_file(path)
    return None


def _record_from_shell_file(path: Path) -> Optional[ScheduledTaskRecord]:
    original_name = path.name.split(".failed", 1)[0].split(".timeout", 1)[0].split(".error", 1)[0]
    run_at = parse_task_filename(original_name)
    if run_at is None:
        return None
    state = TASK_STATE_FAILED if path.parent.name == FAILED_DIRNAME else TASK_STATE_PENDING
    try:
        command = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        command = ""
    return ScheduledTaskRecord(path=path, run_at=run_at, kind=TASK_KIND_COMMAND, state=state, payload={"command": command})


def _record_from_failed_file(path: Path) -> Optional[ScheduledTaskRecord]:
    reason = "failed"
    original_name = path.name
    for candidate_reason in ("timeout", "failed", "error", "invalid", "orphaned"):
        marker = f".{candidate_reason}"
        if marker in path.name:
            reason = candidate_reason
            original_name = path.name.split(marker, 1)[0]
            break
    if original_name.endswith(".sh"):
        record = _record_from_shell_file(path)
        if record is None:
            return None
        return ScheduledTaskRecord(
            path=record.path,
            run_at=record.run_at,
            kind=record.kind,
            state=TASK_STATE_FAILED,
            payload=record.payload,
            reason=reason,
        )
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
    kind = str(payload.get("kind") or TASK_KIND_MESSAGE)
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