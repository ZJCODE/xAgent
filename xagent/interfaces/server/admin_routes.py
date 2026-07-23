"""Monitoring and admin routes for the HTTP server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

import yaml as pyyaml
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from ...interfaces.cli.config_editor import validate_config, write_config
from .models import (
    ConfigInput,
    IdentityInput,
    JobCreateInput,
    SkillCreateInput,
    SkillEntryCreateInput,
    SkillEntryMoveInput,
    SkillStateInput,
    SkillWriteInput,
    TaskCreateInput,
    TaskDuplicateInput,
    TaskUpdateInput,
    WorkspaceWriteInput,
)
from .serializers import message_item, message_search_result
from ...core.runtime import (
    count_archived_job_records,
    count_archived_task_records,
    delete_job,
    delete_scheduled_task,
    duplicate_archived_task,
    enqueue_job,
    enqueue_scheduled_task,
    get_job,
    get_scheduled_task,
    list_archived_job_records,
    list_archived_task_records,
    list_job_records,
    list_task_records,
    pause_scheduled_task,
    request_job_cancel,
    resolve_scheduled_task_run_at,
    resume_scheduled_task,
    update_scheduled_task,
)
from ...schemas.attachment import (
    DEFAULT_WEB_ATTACHMENT_DIR,
    DEFAULT_WEB_IMAGE_DIR,
    MAX_ATTACHMENT_BYTES,
    safe_attachment_filename,
)
from ...tools.image_generation_tool import normalize_image_generation_provider
from ...utils.image_utils import (
    MAX_IMAGE_BYTES,
    SUPPORTED_UPLOAD_IMAGE_MIME_TYPES,
    detect_image_mime,
    workspace_blob_url,
)

if TYPE_CHECKING:
    from .admin_service import AdminService


SENSITIVE_FIELD_SUFFIXES = {"api_key", "secret_key", "app_secret"}
MASK_SENTINEL = "****"

WORKSPACE_TEXT_READ_LIMIT = 1_000_000
WORKSPACE_SEARCH_TEXT_LIMIT = 2_000_000


def _resolve_task_schedule(input_data: Any):
    recurrence = input_data.recurrence
    interval_fields = (
        input_data.interval_seconds is not None
        or input_data.duration_seconds is not None
        or input_data.start_at is not None
        or input_data.end_at is not None
    )
    if recurrence is not None and interval_fields:
        raise ValueError("recurrence cannot be combined with interval schedule fields")
    if recurrence is None and interval_fields:
        if input_data.interval_seconds is None:
            raise ValueError("interval_seconds is required for interval tasks")
        if input_data.duration_seconds is None and input_data.end_at is None:
            raise ValueError("interval tasks require a user-provided duration_seconds or end_at")
        interval_rule: Dict[str, Any] = {
            "kind": "interval",
            "every_seconds": input_data.interval_seconds,
        }
        if input_data.start_at is not None:
            interval_rule["start_at"] = input_data.start_at
        if input_data.duration_seconds is not None:
            interval_rule["duration_seconds"] = input_data.duration_seconds
        if input_data.end_at is not None:
            interval_rule["end_at"] = input_data.end_at
        recurrence = [interval_rule]
    return resolve_scheduled_task_run_at(
        run_at=input_data.run_at,
        delay_seconds=input_data.delay_seconds,
        recurrence=recurrence,
    )


def _task_matches_query(view: dict[str, Any], query: str) -> bool:
    needle = query.strip().lower()
    if not needle:
        return True
    return needle in json.dumps(view, ensure_ascii=False, sort_keys=True, default=str).lower()


def _job_matches_query(view: dict[str, Any], query: str) -> bool:
    return _task_matches_query(view, query)


def _mask_sensitive_fields(data: dict) -> dict:
    """Return a deep copy with sensitive values replaced by MASK_SENTINEL."""
    masked: dict = {}
    for key, value in data.items():
        if key in SENSITIVE_FIELD_SUFFIXES and isinstance(value, str) and value:
            masked[key] = MASK_SENTINEL
        elif isinstance(value, dict):
            masked[key] = _mask_sensitive_fields(value)
        else:
            masked[key] = value
    return masked


def _unmask_sensitive_fields(new_data: dict, current_data: dict) -> None:
    """Restore MASK_SENTINEL values from current_data in-place."""
    for key in list(new_data.keys()):
        if key in SENSITIVE_FIELD_SUFFIXES and new_data.get(key) == MASK_SENTINEL:
            if isinstance(current_data, dict):
                current_value = current_data.get(key)
                if current_value:
                    new_data[key] = current_value
    for key in new_data:
        if isinstance(new_data[key], dict) and isinstance(current_data.get(key), dict):
            _unmask_sensitive_fields(new_data[key], current_data[key])


def register_admin_routes(
    app: FastAPI,
    resolve_admin: Callable[[], "AdminService"],
    *,
    workspace_text_limit: int = WORKSPACE_TEXT_READ_LIMIT,
    workspace_search_text_limit: int = WORKSPACE_SEARCH_TEXT_LIMIT,
) -> None:
    @app.get("/api/agent/info", tags=["Monitoring"])
    async def agent_info():
        server = resolve_admin()
        memory_dir = str(server._get_memory_root())
        storage_info = server.message_storage.get_stream_info() if hasattr(server.message_storage, "get_stream_info") else {}
        identity = server._get_agent_identity()
        try:
            identity_path = server._get_identity_path()
            identity_path_value = str(identity_path)
            identity_editable = True
        except HTTPException:
            identity_path_value = ""
            identity_editable = False
        provider_cfg = server.config.get("provider") if isinstance(server.config, dict) else {}
        provider_name = provider_cfg.get("name") if isinstance(provider_cfg, dict) else None
        image_generation_cfg = server.config.get("image_generation") if isinstance(server.config, dict) else {}
        image_generation_provider = "none"
        if isinstance(image_generation_cfg, dict):
            try:
                image_generation_provider = normalize_image_generation_provider(image_generation_cfg.get("provider"))
            except ValueError:
                image_generation_provider = str(image_generation_cfg.get("provider") or "none")
        tool_names = list(server.agent.tools.keys())
        supports_vision = bool(getattr(server.agent, "supports_vision", True))
        skills_root = server._get_skills_root()
        return {
            "provider": provider_name or "",
            "model": server.agent.model,
            "workspace": str(getattr(server, "workspace", "")),
            "workspace_dir": str(server._get_workspace_root()),
            "skills_dir": str(skills_root),
            "memory_dir": memory_dir,
            "message_storage": storage_info,
            "tools": tool_names,
            "capabilities": {
                "vision": supports_vision,
                "vision_input": supports_vision,
                "web_search": "web_search" in tool_names,
                "image_generation": "generate_image" in tool_names,
                "image_generation_provider": image_generation_provider if "generate_image" in tool_names else "none",
                "image_editing": False,
            },
            "identity": identity,
            "identity_file": server.identity_path.name if hasattr(server, "identity_path") else "identity.md",
            "identity_path": identity_path_value,
            "identity_editable": identity_editable,
            "system_prompt": identity,
        }

    @app.get("/api/tasks", tags=["Monitoring"])
    async def tasks_list(
        scope: str = Query("current", pattern="^(current|scheduled|attention|archive)$"),
        query: str = Query(""),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        server = resolve_admin()
        current_records = list_task_records(server.tasks_dir, include_running=True)
        scheduled_records = [record for record in current_records if record.status in {"active", "paused"}]
        attention_records = [record for record in current_records if record.status == "failed"]
        archive_records = list_archived_task_records(server.tasks_dir) if scope == "archive" else []
        archive_count = len(archive_records) if scope == "archive" else count_archived_task_records(server.tasks_dir)
        selected = {
            "current": [*scheduled_records, *attention_records],
            "scheduled": scheduled_records,
            "attention": sorted(
                attention_records,
                key=lambda record: str(record.payload.get("failed_at") or ""),
                reverse=True,
            ),
            "archive": archive_records,
        }[scope]
        views = [record.to_task_view() for record in selected]
        filtered = [view for view in views if _task_matches_query(view, query)]
        tasks = filtered[offset : offset + limit]
        return {
            "root": str(server.tasks_dir),
            "tasks": tasks,
            "total": len(filtered),
            "counts": {
                "scheduled": len(scheduled_records),
                "attention": len(attention_records),
                "archive": archive_count,
            },
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(tasks) < len(filtered),
        }

    @app.post("/api/tasks", tags=["Monitoring"], status_code=201)
    async def tasks_create(input_data: TaskCreateInput):
        server = resolve_admin()
        channel = str(input_data.channel or "api").strip().lower() or "api"
        if channel != "api":
            raise HTTPException(
                status_code=400,
                detail="HTTP create currently supports channel=api only; use chat to schedule feishu/weixin/voice tasks",
            )
        user_id = str(input_data.user_id or "web_user").strip() or "web_user"
        target = dict(input_data.target or {})
        target.setdefault("user_id", user_id)
        try:
            scheduled_at, normalized_recurrence = _resolve_task_schedule(input_data)
            task = enqueue_scheduled_task(
                task_type=input_data.task_type,
                content=input_data.content,
                run_at=scheduled_at,
                tasks_dir=server.tasks_dir,
                channel=channel,
                target=target,
                user_id=user_id,
                title=input_data.title or "Reminder",
                recurrence=normalized_recurrence or None,
                source={"source": "http", "client": "web"},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "task": task.to_task_view()}

    @app.post("/api/tasks/{task_id}/pause", tags=["Monitoring"])
    async def tasks_pause(task_id: str):
        server = resolve_admin()
        try:
            existing = get_scheduled_task(server.tasks_dir, task_id)
            if existing.status in {"completed", "failed"}:
                raise HTTPException(status_code=409, detail=f"task is immutable in {existing.status} state")
            task = pause_scheduled_task(server.tasks_dir, task_id)
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "task": task.to_task_view()}

    @app.post("/api/tasks/{task_id}/resume", tags=["Monitoring"])
    async def tasks_resume(task_id: str):
        server = resolve_admin()
        try:
            existing = get_scheduled_task(server.tasks_dir, task_id)
            if existing.status in {"completed", "failed"}:
                raise HTTPException(status_code=409, detail=f"task is immutable in {existing.status} state")
            task = resume_scheduled_task(server.tasks_dir, task_id)
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "task": task.to_task_view()}

    @app.patch("/api/tasks/{task_id}", tags=["Monitoring"])
    async def tasks_update(task_id: str, input_data: TaskUpdateInput):
        server = resolve_admin()
        try:
            existing = get_scheduled_task(server.tasks_dir, task_id)
            if existing.status in {"completed", "failed"}:
                raise HTTPException(status_code=409, detail=f"task is immutable in {existing.status} state")
            task = update_scheduled_task(
                server.tasks_dir,
                task_id,
                title=input_data.title,
                content=input_data.content,
                task_type=input_data.task_type,
                run_at=input_data.run_at,
                delay_seconds=input_data.delay_seconds,
                recurrence=input_data.recurrence,
                interval_seconds=input_data.interval_seconds,
                duration_seconds=input_data.duration_seconds,
                start_at=input_data.start_at,
                end_at=input_data.end_at,
            )
        except HTTPException:
            raise
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "task": task.to_task_view()}

    @app.post("/api/tasks/{task_id}/duplicate", tags=["Monitoring"], status_code=201)
    async def tasks_duplicate(task_id: str, input_data: TaskDuplicateInput):
        server = resolve_admin()
        try:
            scheduled_at, normalized_recurrence = _resolve_task_schedule(input_data)
            if scheduled_at <= datetime.now().replace(microsecond=0):
                raise ValueError("duplicated task must be scheduled in the future")
            task = duplicate_archived_task(
                server.tasks_dir,
                task_id,
                run_at=scheduled_at,
                recurrence=normalized_recurrence or None,
                title=input_data.title,
                content=input_data.content,
                task_type=input_data.task_type,
            )
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            status_code = 409 if "only completed archived" in str(exc) else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        return {"status": "ok", "task": task.to_task_view()}

    @app.delete("/api/tasks/delete", tags=["Monitoring"])
    async def tasks_delete(task_id: str = Query(..., description="Stable scheduled task id")):
        server = resolve_admin()
        try:
            task = delete_scheduled_task(server.tasks_dir, task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "deleted": task.to_task_view()}

    @app.get("/api/jobs", tags=["Monitoring"])
    async def jobs_list(
        scope: str = Query("current", pattern="^(current|running|attention|archive)$"),
        query: str = Query(""),
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
    ):
        server = resolve_admin()
        current_records = list_job_records(server.jobs_dir, include_failed=True, include_claimed=True)
        running_records = [record for record in current_records if record.status == "running"]
        queued_records = [record for record in current_records if record.status == "queued"]
        active_records = [*queued_records, *running_records]
        attention_records = [record for record in current_records if record.status == "failed"]
        archive_records = list_archived_job_records(server.jobs_dir) if scope == "archive" else []
        archive_count = len(archive_records) if scope == "archive" else count_archived_job_records(server.jobs_dir)
        selected = {
            "current": [*active_records, *attention_records],
            "running": active_records,
            "attention": sorted(
                attention_records,
                key=lambda record: str(record.payload.get("failed_at") or ""),
                reverse=True,
            ),
            "archive": archive_records,
        }[scope]
        views = [record.to_job_view() for record in selected]
        filtered = [view for view in views if _job_matches_query(view, query)]
        jobs = filtered[offset : offset + limit]
        return {
            "root": str(server.jobs_dir),
            "jobs": jobs,
            "total": len(filtered),
            "counts": {
                "running": len(active_records),
                "queued": len(queued_records),
                "attention": len(attention_records),
                "archive": archive_count,
            },
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(jobs) < len(filtered),
        }

    @app.post("/api/jobs", tags=["Monitoring"], status_code=201)
    async def jobs_create(input_data: JobCreateInput):
        server = resolve_admin()
        channel = str(input_data.channel or "api").strip().lower() or "api"
        if channel != "api":
            raise HTTPException(
                status_code=400,
                detail="HTTP create currently supports channel=api only; use chat to start feishu/weixin/voice jobs",
            )
        user_id = str(input_data.user_id or "web_user").strip() or "web_user"
        target = dict(input_data.target or {})
        target.setdefault("user_id", user_id)
        try:
            job = enqueue_job(
                kind="process",
                command=input_data.command,
                jobs_dir=server.jobs_dir,
                channel=channel,
                target=target,
                user_id=user_id,
                title=input_data.title or "Background job",
                cwd=input_data.cwd,
                timeout_seconds=input_data.timeout_seconds,
                resources=input_data.resources,
                source={"source": "http", "client": "web"},
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        wake = getattr(getattr(server, "api", None), "wake_jobs", None)
        if callable(wake):
            wake()
        return {"status": "ok", "job": job.to_job_view()}

    @app.get("/api/jobs/{job_id}", tags=["Monitoring"])
    async def jobs_get(job_id: str):
        server = resolve_admin()
        try:
            job = get_job(server.jobs_dir, job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "job": job.to_job_view(log_tail=True)}

    @app.post("/api/jobs/{job_id}/cancel", tags=["Monitoring"])
    async def jobs_cancel(job_id: str):
        server = resolve_admin()
        try:
            job = request_job_cancel(server.jobs_dir, job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        wake = getattr(getattr(server, "api", None), "wake_jobs", None)
        if callable(wake):
            wake()
        return {"status": "ok", "job": job.to_job_view()}

    @app.delete("/api/jobs/delete", tags=["Monitoring"])
    async def jobs_delete(job_id: str = Query(..., description="Stable background job id")):
        server = resolve_admin()
        try:
            job = delete_job(server.jobs_dir, job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "deleted": job.to_job_view()}

    @app.get("/api/agent/identity", tags=["Monitoring"])
    async def agent_identity():
        server = resolve_admin()
        identity_path = server._get_identity_path()
        if not identity_path.is_file():
            raise HTTPException(status_code=404, detail="identity.md not found")
        content = identity_path.read_text(encoding="utf-8")
        return {
            "identity": content,
            "path": str(identity_path),
            "filename": identity_path.name,
            "modified": identity_path.stat().st_mtime,
        }

    @app.put("/api/agent/identity", tags=["Monitoring"])
    async def update_agent_identity(input_data: IdentityInput):
        server = resolve_admin()
        identity = input_data.identity.strip()
        if not identity:
            raise HTTPException(status_code=400, detail="Identity cannot be empty")

        identity_path = server._get_identity_path()
        identity_path.parent.mkdir(parents=True, exist_ok=True)
        file_content = f"{identity}\n"
        identity_path.write_text(file_content, encoding="utf-8")
        server._set_agent_identity(identity)

        return {
            "status": "ok",
            "identity": file_content,
            "path": str(identity_path),
            "filename": identity_path.name,
            "modified": identity_path.stat().st_mtime,
        }

    @app.get("/api/agent/config", tags=["Monitoring"])
    async def agent_config():
        server = resolve_admin()
        config_path = Path(server.config_path).expanduser().resolve()
        if not config_path.is_file():
            raise HTTPException(status_code=404, detail="config.yaml not found")

        masked = _mask_sensitive_fields(server.config)
        yaml_str = pyyaml.safe_dump(masked, sort_keys=False, allow_unicode=False)

        return {
            "config": yaml_str,
            "path": str(config_path),
            "filename": config_path.name,
            "modified": config_path.stat().st_mtime,
        }

    @app.put("/api/agent/config", tags=["Monitoring"])
    async def update_agent_config(input_data: ConfigInput):
        server = resolve_admin()
        try:
            new_data = pyyaml.safe_load(input_data.config)
        except pyyaml.YAMLError as e:
            raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}")

        if not isinstance(new_data, dict):
            raise HTTPException(status_code=400, detail="Config must be a mapping (dictionary)")

        _unmask_sensitive_fields(new_data, server.config)

        try:
            validate_config(new_data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        try:
            write_config(server.config_dir, new_data)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        server.config = new_data

        config_path = Path(server.config_path).expanduser().resolve()
        masked = _mask_sensitive_fields(new_data)
        masked_yaml = pyyaml.safe_dump(masked, sort_keys=False, allow_unicode=False)

        return {
            "status": "ok",
            "config": masked_yaml,
            "path": str(config_path),
            "filename": config_path.name,
            "modified": config_path.stat().st_mtime,
        }

    @app.get("/api/memory/tree", tags=["Monitoring"])
    async def memory_tree():
        server = resolve_admin()
        memory_dir = server._get_memory_root()
        if not memory_dir.is_dir():
            return {"tree": []}

        def _scan(directory: Path, rel_root: Path) -> List[Dict[str, Any]]:
            entries: List[Dict[str, Any]] = []
            try:
                children = sorted(directory.iterdir(), key=lambda p: p.name)
            except PermissionError:
                return entries
            for child in children:
                rel = child.relative_to(rel_root)
                if child.is_dir():
                    entries.append({
                        "name": child.name,
                        "path": str(rel),
                        "type": "dir",
                        "children": _scan(child, rel_root),
                    })
                elif child.suffix == ".md":
                    entries.append({
                        "name": child.name,
                        "path": str(rel),
                        "type": "file",
                        "modified": child.stat().st_mtime,
                    })
            return entries

        tree: List[Dict[str, Any]] = []
        for scope_root in server._memory_scope_roots(memory_dir):
            if scope_root.is_dir():
                tree.append({
                    "name": scope_root.name,
                    "path": scope_root.name,
                    "type": "dir",
                    "children": _scan(scope_root, memory_dir),
                })
        return {"tree": tree}

    @app.get("/api/memory/read", tags=["Monitoring"])
    async def memory_read(path: str = Query(..., description="Relative path inside memory directory")):
        server = resolve_admin()
        memory_dir = server._get_memory_root()
        requested = (memory_dir / path).resolve()
        if not requested.is_relative_to(memory_dir):
            raise HTTPException(status_code=403, detail="Access denied")
        if not requested.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        if requested.suffix != ".md":
            raise HTTPException(status_code=403, detail="Only markdown files can be read")

        content = requested.read_text(encoding="utf-8")
        return {
            "path": path,
            "content": content,
            "modified": requested.stat().st_mtime,
        }

    @app.get("/api/memory/search", tags=["Monitoring"])
    async def memory_search(
        query: str = Query(..., min_length=1, description="Search text for memory file names or file content"),
        limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
    ):
        server = resolve_admin()
        memory_dir = server._get_memory_root()
        needle = query.strip().lower()
        results: List[Dict[str, Any]] = []

        memory_files: List[Path] = []
        for scope_root in server._memory_scope_roots(memory_dir):
            if scope_root.is_dir():
                memory_files.extend(sorted(scope_root.rglob("*.md")))

        for file_path in memory_files:
            if len(results) >= limit:
                break

            relative_path = str(file_path.relative_to(memory_dir))
            file_name = file_path.name
            match_kind: List[str] = []
            snippet = ""

            if needle in file_name.lower() or needle in relative_path.lower():
                match_kind.append("filename")

            try:
                content = file_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content = ""

            if content:
                lower_content = content.lower()
                content_index = lower_content.find(needle)
                if content_index != -1:
                    match_kind.append("content")
                    start = max(0, content_index - 80)
                    end = min(len(content), content_index + len(query) + 120)
                    snippet = content[start:end].replace("\n", " ").strip()

            if match_kind:
                results.append({
                    "path": relative_path,
                    "name": file_name,
                    "matched_in": match_kind,
                    "snippet": snippet,
                    "modified": file_path.stat().st_mtime,
                })

        return {
            "query": query,
            "results": results,
        }

    @app.get("/api/workspace/tree", tags=["Monitoring"])
    async def workspace_tree():
        server = resolve_admin()
        workspace_files = server._workspace_files()
        return {
            "root": str(workspace_files.root),
            "tree": workspace_files.scan_tree(),
        }

    @app.get("/api/workspace/read", tags=["Monitoring"])
    async def workspace_read(path: str = Query(..., description="Relative path inside workspace directory")):
        server = resolve_admin()
        return server._workspace_files().read(path, text_limit=workspace_text_limit)

    @app.get("/api/workspace/blob", tags=["Monitoring"])
    async def workspace_blob(path: str = Query(..., description="Relative path inside workspace directory")):
        server = resolve_admin()
        requested = server._workspace_files().resolve_path(path)
        if not requested.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        mime_type, _ = mimetypes.guess_type(requested.name)
        from fastapi.responses import FileResponse

        return FileResponse(str(requested), media_type=mime_type or "application/octet-stream", filename=requested.name)

    @app.get("/api/workspace/search", tags=["Monitoring"])
    async def workspace_search(
        query: str = Query(..., min_length=1, description="Search text for workspace file names or file content"),
        limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
    ):
        server = resolve_admin()
        results = server._workspace_files().search(query, limit=limit, text_limit=workspace_search_text_limit)
        return {"query": query, "results": results}

    @app.post("/api/workspace/clear", tags=["Monitoring"])
    async def workspace_clear():
        server = resolve_admin()
        deleted_count = server._workspace_files().clear()
        return {
            "status": "ok",
            "message": "Workspace cleared",
            "deleted": deleted_count,
        }

    @app.put("/api/workspace/write", tags=["Monitoring"])
    async def workspace_write(input_data: WorkspaceWriteInput):
        server = resolve_admin()
        metadata = server._workspace_files().write_text(
            input_data.path,
            content=input_data.content,
            create_parents=input_data.create_parents,
        )
        return {"status": "ok", **metadata}

    @app.delete("/api/workspace/delete", tags=["Monitoring"])
    async def workspace_delete(
        path: str = Query(..., description="Relative path inside workspace directory"),
        recursive: bool = Query(False, description="Allow deleting non-empty directories"),
    ):
        server = resolve_admin()
        metadata = server._workspace_files().delete(path, recursive=recursive)
        return {"status": "ok", "deleted": metadata}

    @app.post("/api/workspace/upload", tags=["Monitoring"])
    async def workspace_upload(
        file: UploadFile = File(...),
        path: str = Form("", description="Optional relative target path or directory inside workspace"),
    ):
        server = resolve_admin()
        workspace_files = server._workspace_files()
        raw_target = path.strip()
        filename = safe_attachment_filename(file.filename or "upload.bin")
        content = await file.read()
        content_type = (file.content_type or "").split(";", 1)[0].strip().lower()
        detected_mime_type = detect_image_mime(content)
        guessed_mime_type, _ = mimetypes.guess_type(filename)
        looks_like_image = bool(
            detected_mime_type
            or content_type.startswith("image/")
            or (guessed_mime_type and guessed_mime_type.startswith("image/"))
        )
        if looks_like_image:
            if not detected_mime_type:
                raise HTTPException(status_code=415, detail="Uploaded image data is not a supported PNG, JPEG, or WebP file")
            if len(content) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="Image upload exceeds 10MB")
            if detected_mime_type not in SUPPORTED_UPLOAD_IMAGE_MIME_TYPES:
                allowed = ", ".join(sorted(SUPPORTED_UPLOAD_IMAGE_MIME_TYPES))
                raise HTTPException(status_code=415, detail=f"Unsupported image MIME type; allowed: {allowed}")
        elif len(content) > MAX_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="File upload exceeds 50MB")
        if raw_target:
            requested = workspace_files.resolve_upload_path(raw_target, filename)
        else:
            directory = DEFAULT_WEB_IMAGE_DIR if looks_like_image else DEFAULT_WEB_ATTACHMENT_DIR
            requested = workspace_files.resolve_upload_path(
                f"{directory}/",
                _content_addressed_upload_name(filename, content),
            )
        requested.parent.mkdir(parents=True, exist_ok=True)
        requested.write_bytes(content)
        metadata = workspace_files.metadata(requested)
        return {"status": "ok", **metadata, "blob_url": workspace_blob_url(metadata["path"])}

    @app.get("/api/skills/info", tags=["Monitoring"])
    async def skills_info():
        server = resolve_admin()
        try:
            return server._get_skills_storage().info()
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/skills/tree", tags=["Monitoring"])
    async def skills_tree():
        server = resolve_admin()
        storage = server._get_skills_storage()
        return {
            "root": str(storage.root),
            "tree": storage.tree(),
            "skills": [skill.to_dict() for skill in storage.list_skills(include_disabled=True, include_invalid=True)],
        }

    @app.get("/api/skills/read", tags=["Monitoring"])
    async def skills_read(path: str = Query(..., description="Relative path inside skills directory")):
        server = resolve_admin()
        try:
            return server._get_skills_storage().read_file(path)
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/skills/search", tags=["Monitoring"])
    async def skills_search(
        query: str = Query(..., min_length=1, description="Search text for skill file names or file content"),
        limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
    ):
        server = resolve_admin()
        try:
            return server._get_skills_storage().search(query, limit=limit)
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.post("/api/skills/create", tags=["Monitoring"])
    async def skills_create(input_data: SkillCreateInput):
        server = resolve_admin()
        try:
            skill = server._get_skills_storage().create_skill(
                name=input_data.name.strip(),
                description=input_data.description.strip(),
                body=input_data.body,
                license=input_data.license,
                compatibility=input_data.compatibility,
                metadata=input_data.metadata,
                allowed_tools=input_data.allowed_tools,
            )
            return {"status": "ok", "skill": skill.to_dict()}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.put("/api/skills/write", tags=["Monitoring"])
    async def skills_write(input_data: SkillWriteInput):
        server = resolve_admin()
        try:
            result = server._get_skills_storage().write_file(
                input_data.path,
                input_data.content,
                create_parents=input_data.create_parents,
                expected_revision=input_data.expected_revision,
            )
            return {"status": "ok", **result}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.post("/api/skills/entries", tags=["Monitoring"])
    async def skills_create_entry(input_data: SkillEntryCreateInput):
        server = resolve_admin()
        try:
            result = server._get_skills_storage().create_entry(
                input_data.parent_path,
                input_data.name,
                kind=input_data.kind,
                content=input_data.content,
            )
            return {"status": "ok", "entry": result}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.patch("/api/skills/entries", tags=["Monitoring"])
    async def skills_move_entry(input_data: SkillEntryMoveInput):
        server = resolve_admin()
        try:
            result = server._get_skills_storage().move_entry(
                input_data.path,
                input_data.new_parent_path,
                input_data.new_name,
                expected_revision=input_data.expected_revision,
            )
            return {"status": "ok", "entry": result}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.delete("/api/skills/entries", tags=["Monitoring"])
    async def skills_delete_entry(
        path: str = Query(..., description="Relative path inside a skill package"),
        recursive: bool = Query(False, description="Allow deleting directories recursively"),
        expected_revision: Optional[str] = Query(None, description="Expected file content revision"),
    ):
        server = resolve_admin()
        try:
            result = server._get_skills_storage().delete_entry(
                path,
                recursive=recursive,
                expected_revision=expected_revision,
            )
            return {"status": "ok", "deleted": result}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.delete("/api/skills/delete", tags=["Monitoring"])
    async def skills_delete(
        path: str = Query(..., description="Relative path inside skills directory"),
        recursive: bool = Query(False, description="Allow deleting directories recursively"),
    ):
        server = resolve_admin()
        try:
            deleted = server._get_skills_storage().delete_path(path, recursive=recursive)
            return {"status": "ok", "deleted": deleted}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.put("/api/skills/state", tags=["Monitoring"])
    async def skills_state(input_data: SkillStateInput):
        server = resolve_admin()
        try:
            skill = server._get_skills_storage().set_enabled(input_data.name, input_data.enabled)
            return {"status": "ok", "skill": skill.to_dict()}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/skills/validate", tags=["Monitoring"])
    async def skills_validate(name: Optional[str] = Query(None, description="Optional skill name to validate")):
        server = resolve_admin()
        try:
            storage = server._get_skills_storage()
            if name:
                return storage.validate_skill(name)
            return storage.validate_all()
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/messages", tags=["Monitoring"])
    async def get_messages(
        count: int = Query(50, ge=1, le=500, description="Number of messages to retrieve"),
        offset: int = Query(0, ge=0, description="Number of recent messages to skip"),
    ):
        server = resolve_admin()
        total = await server.message_storage.get_message_count()
        messages = await server.message_storage.get_messages(count=count, offset=offset)
        items = [message_item(msg) for msg in messages]
        items.reverse()
        return {
            "messages": items,
            "total": total,
            "count": count,
            "offset": offset,
            "has_more": offset + count < total,
        }

    @app.get("/api/messages/search", tags=["Monitoring"])
    async def search_messages(
        query: str = Query(..., min_length=1, description="Search text for message content and metadata"),
        limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
    ):
        server = resolve_admin()
        total = await server.message_storage.get_message_count()
        if total <= 0 or not hasattr(server.message_storage, "get_messages"):
            return {"query": query, "results": []}

        messages = await server.message_storage.get_messages(count=total, offset=0)
        results: List[Dict[str, Any]] = []
        for message in reversed(messages):
            match = message_search_result(message, query)
            if match is None:
                continue
            results.append(match)
            if len(results) >= limit:
                break

        return {"query": query, "results": results}

    @app.post("/api/memory/clear", tags=["Monitoring"])
    async def memory_clear():
        server = resolve_admin()
        memory_dir = server._get_memory_root()
        try:
            shutil.rmtree(memory_dir)
        except OSError:
            pass
        memory_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "ok"}

    @app.get("/api/messages/stats", tags=["Monitoring"])
    async def messages_stats():
        server = resolve_admin()
        total = await server.message_storage.get_message_count()
        storage_info = server.message_storage.get_stream_info() if hasattr(server.message_storage, "get_stream_info") else {}
        result: Dict[str, Any] = {"total": total, "storage": storage_info}
        if total > 0:
            oldest_task = server.message_storage.get_messages(count=1, offset=total - 1)
            newest_task = server.message_storage.get_messages(count=1, offset=0)
            oldest, newest = await asyncio.gather(oldest_task, newest_task)
            if oldest:
                result["earliest_timestamp"] = oldest[0].timestamp
            if newest:
                result["latest_timestamp"] = newest[0].timestamp
        return result

    @app.post("/clear_messages", tags=["Monitoring"])
    async def clear_messages():
        server = resolve_admin()
        try:
            await server.message_storage.clear_messages()
            return {
                "status": "success",
                "message": "Message stream cleared",
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to clear messages: {str(exc)}")


def _content_addressed_upload_name(filename: str, content: bytes) -> str:
    safe_name = safe_attachment_filename(filename)
    path = Path(safe_name)
    digest = hashlib.sha1(content).hexdigest()[:12]
    stem = path.stem or "upload"
    return f"{stem}-{digest}{path.suffix}"
