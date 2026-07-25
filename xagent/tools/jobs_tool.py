"""Built-in tool for durable background process jobs."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Optional

from xagent.core.runtime import (
    JobSettings,
    JobStore,
    current_delivery_context,
    ensure_worker_running,
)
from xagent.core.tooling.context import current_tool_call_id
from xagent.utils.tool_decorator import function_tool


def create_manage_jobs_tool(
    *,
    jobs_dir: str,
    workspace_dir: Optional[str] = None,
    settings: Optional[JobSettings] = None,
    wake: Optional[Callable[[], None]] = None,
):
    """Create the channel-independent background-jobs tool."""
    job_root = Path(jobs_dir).expanduser().resolve()
    workspace_root = (
        Path(workspace_dir).expanduser().resolve()
        if workspace_dir
        else (job_root.parent / "workspace").resolve()
    )
    resolved_settings = settings or JobSettings()

    @function_tool(
        name="manage_jobs",
        description=(
            "Start, list, inspect, cancel, retry, or delete durable local background jobs. "
            "Jobs continue independently from chat and channel processes. Prefer argv for "
            "safe execution; a raw command requires shell=true."
        ),
        param_descriptions={
            "action": "'start', 'list', 'status', 'cancel', 'retry', or 'delete'.",
            "argv": "Preferred executable and arguments for start, e.g. ['python3', 'script.py'].",
            "command": "Raw shell command for start; requires shell=true.",
            "shell": "Explicitly allow shell parsing for command mode.",
            "title": "Optional short label.",
            "cwd": "Optional working directory inside the agent workspace.",
            "timeout_seconds": "Optional positive timeout. Omit for no timeout.",
            "resources": "Optional exclusive resource names, e.g. ['serial:dmx'].",
            "job_id": "Job id for status, cancel, retry, or delete.",
            "scope": "List scope: active, attention, or history. Defaults to active.",
            "query": "Optional text filter for list.",
            "limit": "Maximum list results. Defaults to 50.",
        },
    )
    def manage_jobs(
        action: Literal["start", "list", "status", "cancel", "retry", "delete"],
        argv: Optional[list[str]] = None,
        command: Optional[str] = None,
        shell: Optional[bool] = None,
        title: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        resources: Optional[list[str]] = None,
        job_id: Optional[str] = None,
        scope: Optional[Literal["active", "attention", "history"]] = None,
        query: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> dict:
        store = JobStore(
            job_root,
            workspace_dir=workspace_root,
            settings=resolved_settings,
        )
        if action == "list":
            selected_scope = scope or "active"
            records, total = store.list_jobs(
                scope=selected_scope,
                query=query or "",
                limit=max(1, min(int(limit or 50), 100)),
            )
            jobs = [record.to_job_view() for record in records]
            return {
                "ok": True,
                "action": "list",
                "scope": selected_scope,
                "jobs": jobs,
                "total": total,
                "has_more": total > len(jobs),
                "counts": store.counts(),
                "worker": store.worker_health(),
                "jobs_dir": str(job_root),
            }

        if action == "status":
            try:
                job = store.get_job(job_id or "")
            except Exception as exc:
                return {"ok": False, "action": "status", "error": str(exc)}
            return {
                "ok": True,
                "action": "status",
                "job": job.to_job_view(log_tail=True),
                "worker": store.worker_health(),
                "jobs_dir": str(job_root),
            }

        if action == "cancel":
            try:
                job = store.request_cancel(job_id or "")
                worker = ensure_worker_running(
                    job_root,
                    workspace_dir=workspace_root,
                    settings=resolved_settings,
                )
            except Exception as exc:
                return {"ok": False, "action": "cancel", "error": str(exc)}
            if wake is not None:
                wake()
            return {
                "ok": True,
                "action": "cancel",
                "job": job.to_job_view(),
                "worker": worker.to_view(),
                "jobs_dir": str(job_root),
            }

        if action == "retry":
            try:
                call_id = current_tool_call_id()
                stable_call_id = call_id if call_id and call_id != "call_0" else None
                job = store.retry_job(
                    job_id or "",
                    idempotency_scope=f"tool:retry:{job_id or ''}",
                    idempotency_key=stable_call_id,
                )
                worker = ensure_worker_running(
                    job_root,
                    workspace_dir=workspace_root,
                    settings=resolved_settings,
                )
            except Exception as exc:
                return {"ok": False, "action": "retry", "error": str(exc)}
            if wake is not None:
                wake()
            return {
                "ok": True,
                "action": "retry",
                "job": job.to_job_view(),
                "worker": worker.to_view(),
                "jobs_dir": str(job_root),
            }

        if action == "delete":
            try:
                job = store.delete_job(job_id or "")
            except Exception as exc:
                return {"ok": False, "action": "delete", "error": str(exc)}
            return {
                "ok": True,
                "action": "delete",
                "deleted": job.to_job_view(),
                "jobs_dir": str(job_root),
            }

        if action != "start":
            return {
                "ok": False,
                "action": str(action or ""),
                "error": "action must be one of: start, list, status, cancel, retry, delete",
            }

        context = current_delivery_context()
        if context is None:
            channel = "local"
            target: dict = {}
            user_id = ""
            source = {"source": "tool", "warning": "No active delivery context."}
        else:
            channel = context.channel
            target = dict(context.target)
            user_id = context.user_id
            source = {"source": "tool", **dict(context.metadata)}
        call_id = current_tool_call_id()
        stable_call_id = call_id if call_id and call_id != "call_0" else None
        idempotency_scope = f"tool:{channel}:{user_id or 'anonymous'}"

        try:
            job = store.create_job(
                title=title or "Background job",
                argv=argv,
                command=command,
                shell=bool(shell),
                cwd=cwd,
                timeout_seconds=timeout_seconds,
                resources=resources,
                channel=channel,
                target=target,
                user_id=user_id,
                source=source,
                idempotency_scope=idempotency_scope,
                idempotency_key=stable_call_id,
            )
            worker = ensure_worker_running(
                job_root,
                workspace_dir=workspace_root,
                settings=resolved_settings,
            )
        except Exception as exc:
            return {"ok": False, "action": "start", "error": str(exc)}

        if wake is not None:
            wake()
        return {
            "ok": True,
            "action": "start",
            "job": job.to_job_view(),
            "worker": worker.to_view(),
            "jobs_dir": str(job_root),
        }

    return manage_jobs
