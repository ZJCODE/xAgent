"""Built-in tool for scheduled conversation tasks."""
from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

from xagent.core.config import AgentConfig
from xagent.core.runtime import (
    current_delivery_context,
    delete_scheduled_task,
    enqueue_scheduled_task,
    list_active_task_views,
    resolve_scheduled_task_run_at,
)
from xagent.utils.tool_decorator import function_tool


def create_schedule_task_tool(*, tasks_dir: str):
    """Create a tool that manages scheduled tasks for the active channel."""
    task_root = Path(tasks_dir).expanduser().resolve()

    @function_tool(
        name="manage_scheduled_tasks",
        description=(
            "Create, list, or delete future tasks for the active delivery channel. "
            "Use message tasks for fixed text and agent tasks for due-time work that may need tools or reasoning."
        ),
        param_descriptions={
            "action": "'create', 'list', or 'delete'.",
            "task_type": "'message' for fixed text, or 'agent' for a due-time agent turn.",
            "content": "Text to send or instruction to execute when due.",
            "run_at": "One-time local datetime, e.g. 20260601-143000 or 2026-06-01 14:30:00.",
            "delay_seconds": "One-time delay from now in seconds.",
            "recurrence": "Structured recurrence rules, e.g. daily or weekly dictionaries.",
            "title": "Optional short label.",
            "task_id": "Task id for delete; obtain from list or create.",
        },
    )
    def manage_scheduled_tasks(
        action: Literal["create", "list", "delete"],
        task_type: Optional[Literal["message", "agent"]] = None,
        content: Optional[str] = None,
        run_at: Optional[str] = None,
        delay_seconds: Optional[int] = None,
        recurrence: Optional[list[dict]] = None,
        title: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict:
        if action == "list":
            tasks = list_active_task_views(task_root)
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

        if action != "create":
            return {
                "ok": False,
                "action": str(action or ""),
                "error": "action must be one of: create, list, delete",
            }

        if task_type is None:
            return {
                "ok": False,
                "action": "create",
                "error": "task_type is required for create.",
            }

        try:
            scheduled_at, normalized_recurrence = resolve_scheduled_task_run_at(
                run_at=run_at,
                delay_seconds=delay_seconds,
                recurrence=recurrence,
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
