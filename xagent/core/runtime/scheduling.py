from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from ...utils.tool_decorator import function_tool
from ..prompts import PromptAssembler
from .engine import AgentRuntime
from .state import RuntimeStateStore
from .delivery_context import current_delivery_context
from .types import DELIVERY_CHANNELS, LOCAL_OWNER_PERSON_ID, AgentEvent, Delivery


TASK_STATUSES = {"active", "paused", "running", "completed", "failed"}


@dataclass(frozen=True)
class RuntimeTask:
    task_id: str
    instruction: str
    schedule: dict[str, Any]
    destination: dict[str, Any] | None
    created_source: str
    created_by: str
    status: str
    next_run_at: float | None
    created_at: float
    updated_at: float
    error: str = ""


class RuntimeTaskStore:
    def __init__(self, state: RuntimeStateStore) -> None:
        self.state = state

    async def create(
        self,
        *,
        instruction: str,
        schedule: dict[str, Any],
        destination: dict[str, Any] | None = None,
        created_source: str,
        created_by: str,
    ) -> RuntimeTask:
        return await asyncio.to_thread(
            self._create_sync,
            instruction,
            schedule,
            destination,
            created_source,
            created_by,
        )

    def _create_sync(
        self,
        instruction: str,
        schedule: dict[str, Any],
        destination: dict[str, Any] | None,
        created_source: str,
        created_by: str,
    ) -> RuntimeTask:
        normalized_instruction = str(instruction or "").strip()
        if not normalized_instruction:
            raise ValueError("instruction is required")
        normalized_source = str(created_source or "").strip().lower()
        normalized_creator = str(created_by or "").strip()
        if not normalized_source:
            raise ValueError("created_source is required")
        if not normalized_creator:
            raise ValueError("created_by is required")
        normalized, next_run = normalize_schedule(schedule)
        normalized_destination = normalize_destination(destination)
        now = time.time()
        task = RuntimeTask(
            task_id=uuid.uuid4().hex,
            instruction=normalized_instruction,
            schedule=normalized,
            destination=normalized_destination,
            created_source=normalized_source,
            created_by=normalized_creator,
            status="active",
            next_run_at=next_run,
            created_at=now,
            updated_at=now,
        )
        with self.state._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_tasks(
                    task_id, task_json, status, next_run_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task.task_id,
                    json.dumps(asdict(task), ensure_ascii=False),
                    task.status,
                    task.next_run_at,
                    now,
                    now,
                ),
            )
            connection.commit()
        return task

    async def list(self, *, status: str | None = None) -> list[RuntimeTask]:
        return await asyncio.to_thread(self._list_sync, status)

    def _list_sync(self, status: str | None) -> list[RuntimeTask]:
        query = "SELECT task_json, status, next_run_at FROM runtime_tasks"
        params: tuple[Any, ...] = ()
        if status:
            if status not in TASK_STATUSES:
                raise ValueError(f"unknown task status: {status}")
            query += " WHERE status=?"
            params = (status,)
        query += " ORDER BY created_at ASC"
        with self.state._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._task_from_row(row) for row in rows]

    async def update(self, task_id: str, **changes: Any) -> RuntimeTask:
        return await asyncio.to_thread(self._update_sync, task_id, changes)

    def _update_sync(self, task_id: str, changes: dict[str, Any]) -> RuntimeTask:
        task = self._get_sync(task_id)
        if task.status == "running":
            raise ValueError("a running task cannot be edited")
        data = asdict(task)
        if "instruction" in changes:
            data["instruction"] = str(changes["instruction"] or "").strip()
        if "destination" in changes:
            data["destination"] = normalize_destination(changes["destination"])
        schedule = changes.get("schedule")
        if schedule is not None:
            normalized, next_run = normalize_schedule(schedule)
            data["schedule"] = normalized
            data["next_run_at"] = next_run
            data["status"] = "active"
            data["error"] = ""
        if not str(data["instruction"]).strip():
            raise ValueError("instruction is required")
        data["updated_at"] = time.time()
        updated = RuntimeTask(**data)
        self._write_sync(updated)
        return updated

    async def set_status(self, task_id: str, status: str) -> RuntimeTask:
        return await asyncio.to_thread(self._set_status_sync, task_id, status)

    def _set_status_sync(self, task_id: str, status: str) -> RuntimeTask:
        if status not in TASK_STATUSES:
            raise ValueError(f"unknown task status: {status}")
        task = self._get_sync(task_id)
        if status == "active" and task.status not in {"paused", "failed"}:
            raise ValueError("only paused or failed tasks can be resumed")
        if status == "paused" and task.status != "active":
            raise ValueError("only active tasks can be paused")
        data = asdict(task)
        data["status"] = status
        data["updated_at"] = time.time()
        if status == "active":
            data["error"] = ""
            data["schedule"], data["next_run_at"] = normalize_schedule(
                data["schedule"],
                after=time.time(),
            )
        updated = RuntimeTask(**data)
        self._write_sync(updated)
        return updated

    async def delete(self, task_id: str) -> RuntimeTask:
        return await asyncio.to_thread(self._delete_sync, task_id)

    def _delete_sync(self, task_id: str) -> RuntimeTask:
        task = self._get_sync(task_id)
        if task.status == "running":
            raise ValueError("a running task cannot be deleted")
        with self.state._connect() as connection:
            connection.execute("DELETE FROM runtime_tasks WHERE task_id=?", (task_id,))
            connection.commit()
        return task

    async def claim_due(self) -> RuntimeTask | None:
        return await asyncio.to_thread(self._claim_due_sync)

    def _claim_due_sync(self) -> RuntimeTask | None:
        with self.state._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT task_id, task_json, status, next_run_at
                FROM runtime_tasks
                WHERE status='active' AND next_run_at<=?
                ORDER BY next_run_at ASC, created_at ASC
                LIMIT 1
                """,
                (time.time(),),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            task = self._task_from_row(row)
            data = asdict(task)
            data["status"] = "running"
            data["updated_at"] = time.time()
            running = RuntimeTask(**data)
            connection.execute(
                "UPDATE runtime_tasks SET task_json=?, status='running', updated_at=? WHERE task_id=?",
                (
                    json.dumps(asdict(running), ensure_ascii=False),
                    running.updated_at,
                    running.task_id,
                ),
            )
            connection.commit()
            return running

    async def finish(self, task: RuntimeTask, *, error: str = "") -> RuntimeTask:
        return await asyncio.to_thread(self._finish_sync, task, error)

    def _finish_sync(self, task: RuntimeTask, error: str) -> RuntimeTask:
        current = self._get_sync(task.task_id)
        if current.status != "running":
            return current
        data = asdict(current)
        data["updated_at"] = time.time()
        if error:
            data["status"] = "failed"
            data["error"] = error
        else:
            next_run = next_schedule_time(
                current.schedule,
                after=max(time.time(), current.next_run_at or 0),
            )
            if next_run is None:
                data["status"] = "completed"
                data["next_run_at"] = None
            else:
                data["status"] = "active"
                data["next_run_at"] = next_run
            data["error"] = ""
        updated = RuntimeTask(**data)
        self._write_sync(updated)
        return updated

    async def recover_interrupted(self) -> int:
        return await asyncio.to_thread(self._recover_interrupted_sync)

    def _recover_interrupted_sync(self) -> int:
        now = time.time()
        recovered = 0
        with self.state._connect() as connection:
            rows = connection.execute(
                "SELECT task_json, status, next_run_at FROM runtime_tasks WHERE status='running'"
            ).fetchall()
            for row in rows:
                task = self._task_from_row(row)
                data = asdict(task)
                data["status"] = "failed"
                data["error"] = "runtime interrupted while task was running"
                data["updated_at"] = now
                failed = RuntimeTask(**data)
                connection.execute(
                    """
                    UPDATE runtime_tasks
                    SET task_json=?, status='failed', updated_at=?
                    WHERE task_id=? AND status='running'
                    """,
                    (json.dumps(asdict(failed), ensure_ascii=False), now, task.task_id),
                )
                recovered += 1
            connection.commit()
        return recovered

    def _get_sync(self, task_id: str) -> RuntimeTask:
        with self.state._connect() as connection:
            row = connection.execute(
                "SELECT task_json, status, next_run_at FROM runtime_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown task: {task_id}")
        return self._task_from_row(row)

    def _write_sync(self, task: RuntimeTask) -> None:
        with self.state._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE runtime_tasks
                SET task_json=?, status=?, next_run_at=?, updated_at=?
                WHERE task_id=?
                """,
                (
                    json.dumps(asdict(task), ensure_ascii=False),
                    task.status,
                    task.next_run_at,
                    task.updated_at,
                    task.task_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown task: {task.task_id}")
            connection.commit()

    @staticmethod
    def _task_from_row(row: Any) -> RuntimeTask:
        data = dict(json.loads(row["task_json"]))
        data["status"] = str(row["status"])
        data["next_run_at"] = (
            float(row["next_run_at"]) if row["next_run_at"] is not None else None
        )
        return RuntimeTask(**data)


class RuntimeTaskScheduler:
    def __init__(self, store: RuntimeTaskStore, runtime: AgentRuntime, state: RuntimeStateStore) -> None:
        self.store = store
        self.runtime = runtime
        self.state = state
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            await self.store.recover_interrupted()
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="xagent-task-scheduler")

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self._stop.is_set():
            task = await self.store.claim_due()
            if task is None:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                run_id = uuid.uuid4().hex
                event = AgentEvent.create(
                    event_id=f"task:{task.task_id}:{run_id}",
                    kind="chat",
                    source="scheduler",
                    conversation_id=f"task:{task.task_id}",
                    speaker_id=task.created_by,
                    audience_ids=(task.created_by,),
                    content=PromptAssembler.scheduled_task(task.instruction),
                    metadata={
                        "scheduled_task_id": task.task_id,
                        "created_source": task.created_source,
                    },
                )
                result = await self.runtime.submit_and_wait(event)
                content = _final_content(result)
                if task.destination is not None:
                    if not content.strip():
                        raise ValueError("scheduled task produced no content")
                    await self.state.add_delivery(
                        Delivery.create(
                            event_id=event.event_id,
                            channel=str(task.destination["channel"]),
                            target=dict(task.destination["target"]),
                            payload={"content": content, "task_id": task.task_id},
                        )
                    )
            except Exception as exc:
                await self.store.finish(task, error=str(exc))
            else:
                await self.store.finish(task)


def create_runtime_task_tool(store: RuntimeTaskStore):
    @function_tool(
        name="manage_scheduled_tasks",
        description=(
            "Create, list, update, pause, resume, or delete Agent tasks. Tasks keep "
            "results in the timeline by default. Only set a destination when the user "
            "explicitly asks for outward delivery. Interval tasks require end_at or "
            "duration_seconds."
        ),
    )
    async def manage_scheduled_tasks(
        action: Literal["create", "list", "update", "pause", "resume", "delete"],
        task_id: str | None = None,
        instruction: str | None = None,
        schedule_kind: Literal["once", "daily", "weekly", "interval"] | None = None,
        run_at: str | None = None,
        local_time: str | None = None,
        weekday: int | None = None,
        interval_seconds: int | None = None,
        duration_seconds: int | None = None,
        end_at: str | None = None,
        deliver_to_current_conversation: bool = False,
        destination_channel: Literal["api", "feishu", "weixin", "voice"] | None = None,
        destination_recipient: str | None = None,
        clear_destination: bool = False,
        status: Literal["active", "paused", "running", "completed", "failed"] | None = None,
    ) -> dict[str, Any]:
        try:
            if action == "list":
                tasks = await store.list(status=status)
                return {"ok": True, "tasks": [asdict(task) for task in tasks]}
            if action == "delete":
                return {"ok": True, "deleted": asdict(await store.delete(task_id or ""))}
            if action == "pause":
                return {"ok": True, "task": asdict(await store.set_status(task_id or "", "paused"))}
            if action == "resume":
                return {"ok": True, "task": asdict(await store.set_status(task_id or "", "active"))}

            schedule = None
            if schedule_kind is not None:
                schedule = {
                    "kind": schedule_kind,
                    "run_at": run_at,
                    "local_time": local_time,
                    "weekday": weekday,
                    "interval_seconds": interval_seconds,
                    "duration_seconds": duration_seconds,
                    "end_at": end_at,
                }
            if action == "update":
                changes: dict[str, Any] = {}
                if instruction is not None:
                    changes["instruction"] = instruction
                if schedule is not None:
                    changes["schedule"] = schedule
                if clear_destination:
                    changes["destination"] = None
                elif deliver_to_current_conversation or destination_channel is not None:
                    changes["destination"] = _tool_destination(
                        current_delivery_context(),
                        deliver_to_current_conversation=deliver_to_current_conversation,
                        destination_channel=destination_channel,
                        destination_recipient=destination_recipient,
                    )
                task = await store.update(
                    task_id or "",
                    **changes,
                )
                return {"ok": True, "task": asdict(task)}
            if action != "create":
                raise ValueError("unsupported action")
            if schedule is None:
                raise ValueError("schedule_kind is required")
            context = current_delivery_context()
            destination = _tool_destination(
                context,
                deliver_to_current_conversation=deliver_to_current_conversation,
                destination_channel=destination_channel,
                destination_recipient=destination_recipient,
            )
            task = await store.create(
                instruction=instruction or "",
                schedule=schedule,
                destination=destination,
                created_source=context.source if context is not None else "runtime",
                created_by=(
                    str(context.metadata.get("person_id") or context.user_id)
                    if context is not None
                    else LOCAL_OWNER_PERSON_ID
                ),
            )
            return {"ok": True, "task": asdict(task)}
        except Exception as exc:
            return {"ok": False, "action": action, "error": str(exc)}

    return manage_scheduled_tasks


def normalize_destination(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("destination must be an object or null")
    channel = str(value.get("channel") or "").strip().lower()
    if channel not in DELIVERY_CHANNELS:
        allowed = ", ".join(sorted(DELIVERY_CHANNELS))
        raise ValueError(f"destination channel must be one of {allowed}")
    target = value.get("target")
    if not isinstance(target, dict):
        raise ValueError("destination target must be an object")
    normalized_target = dict(target)
    if channel == "feishu" and not str(normalized_target.get("chat_id") or "").strip():
        raise ValueError("Feishu destination requires target.chat_id")
    if channel in {"api", "weixin"} and not str(
        normalized_target.get("user_id") or ""
    ).strip():
        raise ValueError(f"{channel} destination requires target.user_id")
    return {"channel": channel, "target": normalized_target}


def _tool_destination(
    context: Any,
    *,
    deliver_to_current_conversation: bool,
    destination_channel: str | None,
    destination_recipient: str | None,
) -> dict[str, Any] | None:
    if deliver_to_current_conversation and destination_channel is not None:
        raise ValueError(
            "choose either the current conversation or an explicit destination"
        )
    if deliver_to_current_conversation:
        if context is None or context.channel not in DELIVERY_CHANNELS:
            raise ValueError("the current conversation has no deliverable channel")
        return normalize_destination(
            {"channel": context.channel, "target": dict(context.target)}
        )
    if destination_channel is None:
        return None
    channel = str(destination_channel).strip().lower()
    recipient = str(destination_recipient or "").strip()
    if channel == "voice":
        return normalize_destination({"channel": channel, "target": {}})
    if not recipient:
        raise ValueError("destination_recipient is required")
    target_key = "chat_id" if channel == "feishu" else "user_id"
    return normalize_destination(
        {"channel": channel, "target": {target_key: recipient}}
    )


def normalize_schedule(
    schedule: dict[str, Any],
    *,
    after: float | None = None,
) -> tuple[dict[str, Any], float]:
    kind = str(schedule.get("kind") or "").strip()
    if kind not in {"once", "daily", "weekly", "interval"}:
        raise ValueError("schedule kind must be once, daily, weekly, or interval")
    normalized = {key: value for key, value in schedule.items() if value is not None}
    now = time.time() if after is None else float(after)
    if kind == "once":
        run = _parse_datetime(schedule.get("run_at"))
        if run <= now:
            raise ValueError("run_at must be in the future")
        return normalized, run
    if kind == "daily":
        _parse_clock(schedule.get("local_time"))
        return normalized, _next_daily(str(schedule["local_time"]), now)
    if kind == "weekly":
        weekday = int(schedule.get("weekday"))
        if not 0 <= weekday <= 6:
            raise ValueError("weekday must be 0 (Monday) through 6 (Sunday)")
        _parse_clock(schedule.get("local_time"))
        return normalized, _next_weekly(weekday, str(schedule["local_time"]), now)

    interval = int(schedule.get("interval_seconds") or 0)
    if interval <= 0:
        raise ValueError("interval_seconds must be positive")
    end_at = schedule.get("end_at")
    duration = schedule.get("duration_seconds")
    persisted_end = schedule.get("end_timestamp")
    if persisted_end is None and end_at is None and duration is None:
        raise ValueError("interval requires end_at or duration_seconds")
    if persisted_end is not None:
        end = float(persisted_end)
    elif end_at is not None:
        end = _parse_datetime(end_at)
    else:
        end = now + int(duration)
    if end <= now:
        raise ValueError("interval end must be in the future")
    normalized["end_timestamp"] = end
    next_run = now + interval
    if next_run > end:
        raise ValueError("interval ends before its first run")
    return normalized, next_run


def next_schedule_time(schedule: dict[str, Any], *, after: float) -> float | None:
    kind = schedule["kind"]
    if kind == "once":
        return None
    if kind == "daily":
        return _next_daily(schedule["local_time"], after)
    if kind == "weekly":
        return _next_weekly(int(schedule["weekday"]), schedule["local_time"], after)
    candidate = after + int(schedule["interval_seconds"])
    return candidate if candidate <= float(schedule["end_timestamp"]) else None


def _parse_datetime(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        raise ValueError("datetime is required")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("datetime must be ISO format") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def _parse_clock(value: Any) -> tuple[int, int]:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%H:%M")
    except ValueError as exc:
        raise ValueError("local_time must be HH:MM") from exc
    return parsed.hour, parsed.minute


def _next_daily(clock: str, after: float) -> float:
    hour, minute = _parse_clock(clock)
    current = datetime.fromtimestamp(after).astimezone()
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= after:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def _next_weekly(weekday: int, clock: str, after: float) -> float:
    hour, minute = _parse_clock(clock)
    current = datetime.fromtimestamp(after).astimezone()
    days = (weekday - current.weekday()) % 7
    candidate = (current + timedelta(days=days)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    if candidate.timestamp() <= after:
        candidate += timedelta(days=7)
    return candidate.timestamp()


def _final_content(result: dict[str, Any]) -> str:
    value = ""
    for event in result.get("events", []):
        if event.get("type") == "message_done" and event.get("phase") == "final":
            value = str(event.get("content") or "")
    return value
