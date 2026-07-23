"""Built-in tool for background jobs that run outside chat turns."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Optional

from xagent.core.runtime import (
    current_delivery_context,
    delete_job,
    enqueue_job,
    get_job,
    list_archived_job_records,
    list_job_records,
    request_job_cancel,
)
from xagent.utils.tool_decorator import function_tool


def create_manage_jobs_tool(*, jobs_dir: str, wake: Optional[Callable[[], None]] = None):
    """Create a tool that manages background jobs for the active channel."""
    job_root = Path(jobs_dir).expanduser().resolve()

    @function_tool(
        name="manage_jobs",
        description=(
            "Start, list, inspect, cancel, or delete background jobs. "
            "Use this for long-running process work that should continue independently "
            "while conversation stays responsive. Prefer this over run_command for multi-minute work."
        ),
        param_descriptions={
            "action": "'start', 'list', 'status', 'cancel', or 'delete'.",
            "command": "Shell command to run for start. Required for start.",
            "title": "Optional short label.",
            "cwd": "Optional working directory inside workspace/ or jobs/<id>/work.",
            "timeout_seconds": "Optional positive timeout. Omit for no explicit timeout.",
            "resources": "Optional exclusive resource names, e.g. ['serial:dmx'].",
            "job_id": "Job id for status, cancel, or delete.",
            "scope": "List scope: current, running, attention, or archive. Defaults to current.",
            "query": "Optional text filter for list.",
            "limit": "Maximum list results. Defaults to 50.",
        },
    )
    def manage_jobs(
        action: Literal["start", "list", "status", "cancel", "delete"],
        command: Optional[str] = None,
        title: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        resources: Optional[list[str]] = None,
        job_id: Optional[str] = None,
        scope: Optional[Literal["current", "running", "attention", "archive"]] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        if action == "list":
            selected_scope = scope or "current"
            current_records = list_job_records(job_root, include_failed=True, include_claimed=True)
            if selected_scope == "archive":
                records = list_archived_job_records(job_root)
            elif selected_scope == "running":
                records = [record for record in current_records if record.status == "running"]
            elif selected_scope == "attention":
                records = [record for record in current_records if record.status == "failed"]
            else:
                records = [record for record in current_records if record.status in {"queued", "running", "failed"}]
            needle = str(query or "").strip().lower()
            jobs = [record.to_job_view() for record in records]
            if needle:
                jobs = [job for job in jobs if needle in str(job).lower()]
            result_limit = max(1, min(int(limit or 50), 100))
            total = len(jobs)
            jobs = jobs[:result_limit]
            return {
                "ok": True,
                "action": "list",
                "scope": selected_scope,
                "jobs": jobs,
                "total": total,
                "has_more": total > len(jobs),
                "jobs_dir": str(job_root),
            }

        if action == "status":
            try:
                job = get_job(job_root, job_id or "")
            except Exception as exc:
                return {"ok": False, "action": "status", "error": str(exc)}
            return {
                "ok": True,
                "action": "status",
                "job": job.to_job_view(log_tail=True),
                "jobs_dir": str(job_root),
            }

        if action == "cancel":
            try:
                job = request_job_cancel(job_root, job_id or "")
            except Exception as exc:
                return {"ok": False, "action": "cancel", "error": str(exc)}
            if wake is not None:
                wake()
            return {"ok": True, "action": "cancel", "job": job.to_job_view(), "jobs_dir": str(job_root)}

        if action == "delete":
            try:
                job = delete_job(job_root, job_id or "")
            except Exception as exc:
                return {"ok": False, "action": "delete", "error": str(exc)}
            return {"ok": True, "action": "delete", "deleted": job.to_job_view(), "jobs_dir": str(job_root)}

        if action != "start":
            return {
                "ok": False,
                "action": str(action or ""),
                "error": "action must be one of: start, list, status, cancel, delete",
            }

        context = current_delivery_context()
        if context is None:
            channel = "local"
            target: dict = {}
            user_id = ""
            source = {"warning": "No active channel context was available when this job was created."}
        else:
            channel = context.channel
            target = dict(context.target)
            user_id = context.user_id
            source = dict(context.metadata)

        try:
            job = enqueue_job(
                kind="process",
                command=command or "",
                jobs_dir=job_root,
                channel=channel,
                target=target,
                user_id=user_id,
                title=title or "Background job",
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                resources=resources,
                source=source,
            )
        except Exception as exc:
            return {"ok": False, "action": "start", "error": str(exc)}

        if wake is not None:
            wake()
        return {
            "ok": True,
            "action": "start",
            "job": job.to_job_view(),
            "jobs_dir": str(job_root),
        }

    return manage_jobs
