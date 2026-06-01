"""Built-in tool for conversation scheduled messages."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from xagent.core.runtime import current_delivery_context, enqueue_message_task
from xagent.utils.tool_decorator import function_tool


def create_schedule_message_tool(*, tasks_dir: str):
    """Create a tool that schedules a future message back to the active channel."""
    task_root = Path(tasks_dir).expanduser().resolve()

    @function_tool(
        name="schedule_message",
        description=(
            "Schedule a future assistant message/reminder for the current conversation channel. "
            "Use this when the user asks to be reminded, notified, or messaged later, such as "
            "'一分钟后提醒我走两步'. The active xAgent channel runtime delivers it automatically."
        ),
        param_descriptions={
            "message": "The exact reminder/message text to send later. Keep it concise and useful to the user.",
            "run_at": "Local delivery time, such as 20260601-143000 or 2026-06-01 14:30:00. Optional when delay_seconds is provided.",
            "delay_seconds": "Delay from now in seconds. Optional when run_at is provided.",
            "title": "Optional short label for the task, such as Reminder.",
        },
    )
    def schedule_message(
        message: str,
        run_at: Optional[str] = None,
        delay_seconds: Optional[int] = None,
        title: Optional[str] = None,
    ) -> dict:
        if delay_seconds is None and not run_at:
            return {
                "scheduled": False,
                "error": "Provide either run_at or delay_seconds.",
            }
        if delay_seconds is not None:
            if delay_seconds < 0:
                return {
                    "scheduled": False,
                    "error": "delay_seconds must be zero or positive.",
                }
            scheduled_at: str | datetime = datetime.now().replace(microsecond=0) + timedelta(seconds=delay_seconds)
        else:
            scheduled_at = run_at or ""

        context = current_delivery_context()
        if context is None:
            target = {"channel": "local"}
            user_id = ""
            source = {"warning": "No active channel context was available when this task was created."}
        else:
            target = {"channel": context.channel, "user_id": context.user_id, **context.target}
            user_id = context.user_id
            source = context.metadata

        try:
            task = enqueue_message_task(
                message=message,
                run_at=scheduled_at,
                tasks_dir=task_root,
                target=target,
                user_id=user_id,
                title=title or "Reminder",
                source=source,
            )
        except Exception as exc:
            return {
                "scheduled": False,
                "error": str(exc),
            }

        return {
            "scheduled": True,
            "task": task.name,
            "run_at": task.run_at.strftime("%Y-%m-%d %H:%M:%S"),
            "channel": target.get("channel") or "local",
            "tasks_dir": str(task_root),
        }

    return schedule_message
