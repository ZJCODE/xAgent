"""Built-in tool for scheduled conversation tasks."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional

from xagent.core.config import AgentConfig
from xagent.core.runtime import current_delivery_context, enqueue_scheduled_task
from xagent.utils.tool_decorator import function_tool


def create_schedule_task_tool(*, tasks_dir: str):
    """Create a tool that schedules a future task for the active channel."""
    task_root = Path(tasks_dir).expanduser().resolve()

    @function_tool(
        name="schedule_task",
        description=(
            "Schedule a future task for the current conversation channel. "
            "Use task_type='message' for a direct future reminder/message, and task_type='agent' "
            "when the future task must gather information, call tools, or perform reasoning before replying. "
            "The active xAgent channel runtime executes due tasks and delivers the final result automatically."
        ),
        param_descriptions={
            "task_type": "Use 'message' for direct text delivery, or 'agent' for a due-time agent turn that can call tools before replying.",
            "content": "For message tasks, the exact text to send later. For agent tasks, the instruction to execute when due.",
            "run_at": "Local delivery time, such as 20260601-143000 or 2026-06-01 14:30:00. Optional when delay_seconds is provided.",
            "delay_seconds": "Delay from now in seconds. Optional when run_at is provided.",
            "title": "Optional short label for the task, such as Reminder or Temperature Check.",
        },
    )
    def schedule_task(
        task_type: Literal["message", "agent"],
        content: str,
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
                content=content,
                run_at=scheduled_at,
                tasks_dir=task_root,
                channel=channel,
                target=target,
                user_id=user_id,
                title=title or "Reminder",
                source=source,
                execution={
                    "history_count": AgentConfig.DEFAULT_HISTORY_COUNT,
                    "max_iter": AgentConfig.DEFAULT_MAX_ITER,
                    "max_concurrent_tools": AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
                    "enable_memory": True,
                },
            )
        except Exception as exc:
            return {
                "scheduled": False,
                "error": str(exc),
            }

        return {
            "scheduled": True,
            "task": task.name,
            "task_type": task.task_type,
            "run_at": task.run_at.strftime("%Y-%m-%d %H:%M:%S"),
            "channel": channel or "local",
            "tasks_dir": str(task_root),
        }

    return schedule_task
