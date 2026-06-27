"""Monitoring and admin routes for the HTTP server."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from .models import IdentityInput, SkillCreateInput, SkillStateInput, SkillWriteInput, WorkspaceWriteInput
from .serializers import message_item, message_search_result
from ...core.runtime import delete_scheduled_task, list_task_records
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
    from .app import AgentHTTPServer


def register_admin_routes(
    app: FastAPI,
    server: "AgentHTTPServer",
    *,
    workspace_text_limit: int,
    workspace_search_text_limit: int,
) -> None:
    @app.get("/api/agent/info", tags=["Monitoring"])
    async def agent_info():
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
    async def tasks_list():
        tasks = [record.to_task_view() for record in list_task_records(server.tasks_dir)]
        return {
            "root": str(server.tasks_dir),
            "tasks": tasks,
            "total": len(tasks),
        }

    @app.delete("/api/tasks/delete", tags=["Monitoring"])
    async def tasks_delete(task_id: str = Query(..., description="Stable scheduled task id")):
        try:
            task = delete_scheduled_task(server.tasks_dir, task_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"status": "ok", "deleted": task.to_task_view()}

    @app.get("/api/agent/identity", tags=["Monitoring"])
    async def agent_identity():
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

    @app.get("/api/memory/tree", tags=["Monitoring"])
    async def memory_tree():
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
            except OSError:
                continue

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
        workspace_files = server._workspace_files()
        return {
            "root": str(workspace_files.root),
            "tree": workspace_files.scan_tree(),
        }

    @app.get("/api/workspace/read", tags=["Monitoring"])
    async def workspace_read(path: str = Query(..., description="Relative path inside workspace directory")):
        return server._workspace_files().read(path, text_limit=workspace_text_limit)

    @app.get("/api/workspace/blob", tags=["Monitoring"])
    async def workspace_blob(path: str = Query(..., description="Relative path inside workspace directory")):
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
        results = server._workspace_files().search(query, limit=limit, text_limit=workspace_search_text_limit)
        return {"query": query, "results": results}

    @app.post("/api/workspace/clear", tags=["Monitoring"])
    async def workspace_clear():
        deleted_count = server._workspace_files().clear()
        return {
            "status": "ok",
            "message": "Workspace cleared",
            "deleted": deleted_count,
        }

    @app.put("/api/workspace/write", tags=["Monitoring"])
    async def workspace_write(input_data: WorkspaceWriteInput):
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
        metadata = server._workspace_files().delete(path, recursive=recursive)
        return {"status": "ok", "deleted": metadata}

    @app.post("/api/workspace/upload", tags=["Monitoring"])
    async def workspace_upload(
        file: UploadFile = File(...),
        path: str = Form("", description="Optional relative target path or directory inside workspace"),
    ):
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
        try:
            return server._get_skills_storage().info()
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/skills/tree", tags=["Monitoring"])
    async def skills_tree():
        storage = server._get_skills_storage()
        return {
            "root": str(storage.root),
            "tree": storage.tree(),
            "skills": [skill.to_dict() for skill in storage.list_skills(include_disabled=True, include_invalid=True)],
        }

    @app.get("/api/skills/read", tags=["Monitoring"])
    async def skills_read(path: str = Query(..., description="Relative path inside skills directory")):
        try:
            return server._get_skills_storage().read_file(path)
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/skills/search", tags=["Monitoring"])
    async def skills_search(
        query: str = Query(..., min_length=1, description="Search text for skill file names or file content"),
        limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
    ):
        try:
            return server._get_skills_storage().search(query, limit=limit)
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.post("/api/skills/create", tags=["Monitoring"])
    async def skills_create(input_data: SkillCreateInput):
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
        try:
            result = server._get_skills_storage().write_file(
                input_data.path,
                input_data.content,
                create_parents=input_data.create_parents,
            )
            return {"status": "ok", **result}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.delete("/api/skills/delete", tags=["Monitoring"])
    async def skills_delete(
        path: str = Query(..., description="Relative path inside skills directory"),
        recursive: bool = Query(False, description="Allow deleting directories recursively"),
    ):
        try:
            deleted = server._get_skills_storage().delete_path(path, recursive=recursive)
            return {"status": "ok", "deleted": deleted}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.put("/api/skills/state", tags=["Monitoring"])
    async def skills_state(input_data: SkillStateInput):
        try:
            skill = server._get_skills_storage().set_enabled(input_data.name, input_data.enabled)
            return {"status": "ok", "skill": skill.to_dict()}
        except Exception as exc:
            server._raise_skills_http_error(exc)

    @app.get("/api/skills/validate", tags=["Monitoring"])
    async def skills_validate(name: Optional[str] = Query(None, description="Optional skill name to validate")):
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
        memory_dir = server._get_memory_root()
        try:
            shutil.rmtree(memory_dir)
        except OSError:
            pass
        memory_dir.mkdir(parents=True, exist_ok=True)
        return {"status": "ok"}

    @app.get("/api/messages/stats", tags=["Monitoring"])
    async def messages_stats():
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


def _content_addressed_upload_name(filename: str, content: bytes) -> str:
    safe_name = safe_attachment_filename(filename)
    path = Path(safe_name)
    digest = hashlib.sha1(content).hexdigest()[:12]
    stem = path.stem or "upload"
    return f"{stem}-{digest}{path.suffix}"
