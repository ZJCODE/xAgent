"""Built-in tool for scheduled conversation tasks."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from xagent.core.runtime import (
    current_delivery_context,
    delete_scheduled_task,
    enqueue_scheduled_task,
    list_task_records,
    pause_scheduled_task,
    resolve_scheduled_task_run_at,
    resume_scheduled_task,
    update_scheduled_task,
)
from xagent.utils.tool_decorator import function_tool


def create_schedule_task_tool(*, tasks_dir: str):
    """Create a tool that manages scheduled tasks for the active channel."""
    task_root = Path(tasks_dir).expanduser().resolve()

    @function_tool(
        name="manage_scheduled_tasks",
        description=(
            "Create, list, update, pause, resume, or delete future tasks for the active delivery channel. "
            "Use message tasks for fixed text, agent tasks for due-time work that may need tools or reasoning, "
            "and interval schedules for bounded repeated reminders. "
            "Interval tasks require an explicit user-provided duration_seconds or end_at; "
            "if missing, ask the user and do not invent a default. "
            "Prefer pause over delete for temporary stops; use update to extend end_at or change content."
        ),
        param_descriptions={
            "action": "'create', 'list', 'update', 'pause', 'resume', or 'delete'.",
            "task_type": "'message' for fixed text, or 'agent' for a due-time agent turn.",
            "content": "Text to send or instruction to execute when due.",
            "run_at": "One-time local datetime, e.g. 20260601-143000 or 2026-06-01 14:30:00.",
            "delay_seconds": "One-time delay from now in seconds, or first run delay for interval schedules.",
            "recurrence": "Structured recurrence rules, e.g. daily, weekly, or interval dictionaries.",
            "interval_seconds": "Repeat every N seconds. Requires user-provided duration_seconds or end_at.",
            "duration_seconds": "Required for interval unless end_at is set. Must come from the user; do not invent.",
            "start_at": "Optional interval window start. Use for requests like from 10:00 to 12:00 every 10 minutes.",
            "end_at": "Required for interval unless duration_seconds is set. Must come from the user; do not invent.",
            "title": "Optional short label.",
            "task_id": "Task id for update, pause, resume, or delete; obtain from list or create.",
        },
    )
    def manage_scheduled_tasks(
        action: Literal["create", "list", "update", "pause", "resume", "delete"],
        task_type: Optional[Literal["message", "agent"]] = None,
        content: Optional[str] = None,
        run_at: Optional[str] = None,
        delay_seconds: Optional[int] = None,
        recurrence: Optional[list[dict]] = None,
        interval_seconds: Optional[int] = None,
        duration_seconds: Optional[int] = None,
        start_at: Optional[str] = None,
        end_at: Optional[str] = None,
        title: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict:
        if action == "list":
            tasks = [r.to_task_view() for r in list_task_records(task_root)]
            return {
                "ok": True,
                "action": "list",
                "tasks": tasks,
                "total": len(tasks),
                "tasks_dir": str(task_root),
            }

        if action == "delete":
            try:
                task = delete_scheduled_task(task_root, task_id or "")
            except Exception as exc:
                return {
                    "ok": False,
                    "action": "delete",
                    "error": str(exc),
                }
            return {
                "ok": True,
                "action": "delete",
                "deleted": task.to_task_view(),
                "tasks_dir": str(task_root),
            }

        if action == "pause":
            try:
                task = pause_scheduled_task(task_root, task_id or "")
            except Exception as exc:
                return {"ok": False, "action": "pause", "error": str(exc)}
            return {"ok": True, "action": "pause", "task": task.to_task_view(), "tasks_dir": str(task_root)}

        if action == "resume":
            try:
                task = resume_scheduled_task(task_root, task_id or "")
            except Exception as exc:
                return {"ok": False, "action": "resume", "error": str(exc)}
            return {"ok": True, "action": "resume", "task": task.to_task_view(), "tasks_dir": str(task_root)}

        if action == "update":
            try:
                task = update_scheduled_task(
                    task_root,
                    task_id or "",
                    title=title,
                    content=content,
                    task_type=task_type,
                    run_at=run_at,
                    delay_seconds=delay_seconds,
                    recurrence=recurrence,
                    interval_seconds=interval_seconds,
                    duration_seconds=duration_seconds,
                    start_at=start_at,
                    end_at=end_at,
                )
            except Exception as exc:
                return {"ok": False, "action": "update", "error": str(exc)}
            return {"ok": True, "action": "update", "task": task.to_task_view(), "tasks_dir": str(task_root)}

        if action != "create":
            return {
                "ok": False,
                "action": str(action or ""),
                "error": "action must be one of: create, list, update, pause, resume, delete",
            }

        if task_type is None:
            return {
                "ok": False,
                "action": "create",
                "error": "task_type is required for create.",
            }

        try:
            recurrence_for_create = recurrence
            interval_fields_provided = (
                interval_seconds is not None
                or duration_seconds is not None
                or start_at is not None
                or end_at is not None
            )
            if recurrence is not None and interval_fields_provided:
                raise ValueError("recurrence cannot be combined with interval_seconds, duration_seconds, or end_at")
            if recurrence is None and interval_fields_provided:
                if interval_seconds is None:
                    raise ValueError("interval_seconds is required for interval tasks")
                if duration_seconds is None and end_at is None:
                    raise ValueError(
                        "interval tasks require a user-provided duration_seconds or end_at; "
                        "ask the user how long to continue or when to stop before creating"
                    )
                interval_rule: dict = {"kind": "interval", "every_seconds": interval_seconds}
                if start_at is not None:
                    interval_rule["start_at"] = start_at
                if duration_seconds is not None:
                    interval_rule["duration_seconds"] = duration_seconds
                if end_at is not None:
                    interval_rule["end_at"] = end_at
                recurrence_for_create = [interval_rule]
            scheduled_at, normalized_recurrence = resolve_scheduled_task_run_at(
                run_at=run_at,
                delay_seconds=delay_seconds,
                recurrence=recurrence_for_create,
            )
        except Exception as exc:
            return {
                "ok": False,
                "action": "create",
                "error": str(exc),
            }

        context = current_delivery_context()
        if context is None:
            channel = "local"
            target = {}
            user_id = ""
            source = {"warning": "No active channel context was available when this task was created."}
        else:
            channel = context.channel
            target = dict(context.target)
            user_id = context.user_id
            source = context.metadata

        try:
            task = enqueue_scheduled_task(
                task_type=task_type,
                content=content or "",
                run_at=scheduled_at,
                tasks_dir=task_root,
                channel=channel,
                target=target,
                user_id=user_id,
                title=title or "Reminder",
                recurrence=normalized_recurrence or None,
                source=source,
            )
        except Exception as exc:
            return {
                "ok": False,
                "action": "create",
                "error": str(exc),
            }

        return {
            "ok": True,
            "action": "create",
            "task": task.to_task_view(),
            "tasks_dir": str(task_root),
        }

    return manage_scheduled_tasks
