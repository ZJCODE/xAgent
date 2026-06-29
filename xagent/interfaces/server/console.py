"""Global Web Console server for managing xAgent agents."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import uvicorn
import websockets
from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, StreamingResponse

from ..base import BaseAgentConfig
from .console_services import (
    CONSOLE_PORT,
    AgentConfigService,
    AgentDataService,
    AgentRegistryService,
    ChannelService,
    SetupSessionService,
)
from .models import IdentityInput, SkillCreateInput, SkillStateInput, SkillWriteInput, WorkspaceWriteInput
from .web import register_spa_routes


_STATIC_DIR = Path(__file__).parent.parent / "static"
_WORKSPACE_TEXT_READ_LIMIT = 1_000_000
_WORKSPACE_SEARCH_TEXT_LIMIT = 2_000_000


class ConsoleHTTPServer:
    """HTTP server for the global xAgent management Console."""

    def __init__(self, enable_web: bool = True):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._enable_web = enable_web
        self.registry = AgentRegistryService()
        self.config = AgentConfigService(self.registry)
        self.channels = ChannelService(self.registry)
        self.data = AgentDataService(self.registry)
        self.setup_sessions = SetupSessionService(self.registry, self.config)
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent Console",
            description="Global management console for local xAgent agents",
            version="1.0.0",
            lifespan=self._lifespan,
        )
        self._add_routes(app)
        if self._enable_web:
            register_spa_routes(app, static_dir=_STATIC_DIR, logger=self.logger)
        return app

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        del app
        yield

    def _add_routes(self, app: FastAPI) -> None:
        @app.get("/i/health", tags=["Health"])
        async def health_check():
            return "ok"

        @app.get("/health", tags=["Health"])
        async def health():
            return {"status": "healthy", "service": "xAgent Console"}

        @app.get("/api/console/agents", tags=["Console"])
        async def agents_list():
            return self.registry.list_agents(self.channels)

        @app.post("/api/console/agents", tags=["Console"])
        async def agents_create(payload: dict[str, Any] = Body(...)):
            result = self.registry.create_agent(payload)
            return {**result, **self.registry.list_agents(self.channels)}

        @app.get("/api/console/agents/{name}", tags=["Console"])
        async def agents_get(name: str):
            registry, entry = self.registry.require_entry(name)
            return {"agent": self.registry.summary_for_entry(registry, entry, channel_service=self.channels)}

        @app.patch("/api/console/agents/{name}", tags=["Console"])
        async def agents_update(name: str, payload: dict[str, Any] = Body(...)):
            return self.registry.update_agent(name, payload)

        @app.post("/api/console/agents/{name}/select", tags=["Console"])
        async def agents_select(name: str):
            return self.registry.select_agent(name)

        @app.delete("/api/console/agents/{name}", tags=["Console"])
        async def agents_delete(
            name: str,
            stop_running_channels: bool = Query(False, description="Stop running channels before deletion"),
        ):
            return self.registry.delete_agent(name, stop_running_channels=stop_running_channels)

        @app.get("/api/console/agents/{name}/overview", tags=["Console"])
        async def agent_overview(name: str):
            return self.config.overview(name)

        @app.get("/api/console/agents/{name}/identity", tags=["Console"])
        async def agent_identity(name: str):
            return self.config.read_identity(name)

        @app.put("/api/console/agents/{name}/identity", tags=["Console"])
        async def agent_identity_update(name: str, input_data: IdentityInput):
            return self.config.write_identity(name, input_data.identity)

        @app.get("/api/console/agents/{name}/config", tags=["Console"])
        async def agent_config(name: str):
            return self.config.read_config(name)

        @app.post("/api/console/agents/{name}/config/preview", tags=["Console"])
        async def agent_config_preview(name: str, payload: dict[str, Any] = Body(...)):
            candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
            return self.config.preview_config(name, candidate)

        @app.put("/api/console/agents/{name}/config", tags=["Console"])
        async def agent_config_update(name: str, payload: dict[str, Any] = Body(...)):
            candidate = payload.get("config") if isinstance(payload.get("config"), dict) else payload
            return self.config.write_config(name, candidate)

        @app.get("/api/console/agents/{name}/channels", tags=["Console"])
        async def channels_list(name: str):
            return {"channels": self.channels.channel_states(name)}

        @app.post("/api/console/agents/{name}/channels/{channel}/start", tags=["Console"])
        async def channel_start(name: str, channel: str):
            return self.channels.start_channel(name, channel)

        @app.post("/api/console/agents/{name}/channels/{channel}/stop", tags=["Console"])
        async def channel_stop(name: str, channel: str):
            return self.channels.stop_channel(name, channel)

        @app.post("/api/console/agents/{name}/channels/{channel}/restart", tags=["Console"])
        async def channel_restart(name: str, channel: str):
            return self.channels.restart_channel(name, channel)

        @app.get("/api/console/agents/{name}/channels/{channel}/logs", tags=["Console"])
        async def channel_logs(
            name: str,
            channel: str,
            lines: int = Query(120, ge=1, le=1000),
        ):
            return self.channels.logs(name, channel, lines=lines)

        self._add_agent_data_routes(app)
        self._add_setup_session_routes(app)
        self._add_websocket_routes(app)

    def _add_agent_data_routes(self, app: FastAPI) -> None:
        @app.get("/api/console/agents/{name}/memory/tree", tags=["Console Data"])
        async def memory_tree(name: str):
            return self.data.memory_tree(name)

        @app.get("/api/console/agents/{name}/memory/read", tags=["Console Data"])
        async def memory_read(name: str, path: str = Query(...)):
            return self.data.memory_read(name, path)

        @app.get("/api/console/agents/{name}/memory/search", tags=["Console Data"])
        async def memory_search(name: str, query: str = Query(..., min_length=1)):
            return self.data.memory_search(name, query)

        @app.post("/api/console/agents/{name}/memory/clear", tags=["Console Data"])
        async def memory_clear(name: str):
            return self.data.memory_clear(name)

        @app.get("/api/console/agents/{name}/workspace/tree", tags=["Console Data"])
        async def workspace_tree(name: str):
            files = self.data.workspace_files(name)
            return {"root": str(files.root), "tree": files.scan_tree()}

        @app.get("/api/console/agents/{name}/workspace/read", tags=["Console Data"])
        async def workspace_read(name: str, path: str = Query(...)):
            result = self.data.workspace_files(name).read(path, text_limit=_WORKSPACE_TEXT_READ_LIMIT)
            return self._rewrite_blob_payload(name, result)

        @app.get("/api/console/agents/{name}/workspace/blob", tags=["Console Data"])
        async def workspace_blob(name: str, path: str = Query(...)):
            requested = self.data.workspace_files(name).resolve_path(path)
            if not requested.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            import mimetypes

            mime_type, _ = mimetypes.guess_type(requested.name)
            return FileResponse(str(requested), media_type=mime_type or "application/octet-stream", filename=requested.name)

        @app.get("/api/console/agents/{name}/workspace/search", tags=["Console Data"])
        async def workspace_search(name: str, query: str = Query(..., min_length=1)):
            results = self.data.workspace_files(name).search(query, limit=50, text_limit=_WORKSPACE_SEARCH_TEXT_LIMIT)
            return self._rewrite_blob_payload(name, {"query": query, "results": results})

        @app.post("/api/console/agents/{name}/workspace/clear", tags=["Console Data"])
        async def workspace_clear(name: str):
            deleted = self.data.workspace_files(name).clear()
            return {"status": "ok", "message": "Workspace cleared", "deleted": deleted}

        @app.put("/api/console/agents/{name}/workspace/write", tags=["Console Data"])
        async def workspace_write(name: str, input_data: WorkspaceWriteInput):
            metadata = self.data.workspace_files(name).write_text(
                input_data.path,
                content=input_data.content,
                create_parents=input_data.create_parents,
            )
            return {"status": "ok", **metadata}

        @app.delete("/api/console/agents/{name}/workspace/delete", tags=["Console Data"])
        async def workspace_delete(name: str, path: str = Query(...), recursive: bool = Query(False)):
            deleted = self.data.workspace_files(name).delete(path, recursive=recursive)
            return {"status": "ok", "deleted": deleted}

        @app.post("/api/console/agents/{name}/workspace/upload", tags=["Console Data"])
        async def workspace_upload(
            name: str,
            file: UploadFile = File(...),
            path: str = Form("", description="Optional relative target path or directory inside workspace"),
        ):
            return await self.data.workspace_upload(name, file, path)

        @app.get("/api/console/agents/{name}/skills/info", tags=["Console Data"])
        async def skills_info(name: str):
            return self.data.skills_storage(name).info()

        @app.get("/api/console/agents/{name}/skills/tree", tags=["Console Data"])
        async def skills_tree(name: str):
            storage = self.data.skills_storage(name)
            return {
                "root": str(storage.root),
                "tree": storage.tree(),
                "skills": [skill.to_dict() for skill in storage.list_skills(include_disabled=True, include_invalid=True)],
            }

        @app.get("/api/console/agents/{name}/skills/read", tags=["Console Data"])
        async def skills_read(name: str, path: str = Query(...)):
            return self.data.skills_storage(name).read_file(path)

        @app.get("/api/console/agents/{name}/skills/search", tags=["Console Data"])
        async def skills_search(name: str, query: str = Query(..., min_length=1)):
            return self.data.skills_storage(name).search(query)

        @app.post("/api/console/agents/{name}/skills/create", tags=["Console Data"])
        async def skills_create(name: str, input_data: SkillCreateInput):
            skill = self.data.skills_storage(name).create_skill(
                name=input_data.name.strip(),
                description=input_data.description.strip(),
                body=input_data.body,
                license=input_data.license,
                compatibility=input_data.compatibility,
                metadata=input_data.metadata,
                allowed_tools=input_data.allowed_tools,
            )
            return {"status": "ok", "skill": skill.to_dict()}

        @app.put("/api/console/agents/{name}/skills/write", tags=["Console Data"])
        async def skills_write(name: str, input_data: SkillWriteInput):
            return {"status": "ok", **self.data.skills_storage(name).write_file(input_data.path, input_data.content, create_parents=input_data.create_parents)}

        @app.delete("/api/console/agents/{name}/skills/delete", tags=["Console Data"])
        async def skills_delete(name: str, path: str = Query(...), recursive: bool = Query(False)):
            return {"status": "ok", "deleted": self.data.skills_storage(name).delete_path(path, recursive=recursive)}

        @app.put("/api/console/agents/{name}/skills/state", tags=["Console Data"])
        async def skills_state(name: str, input_data: SkillStateInput):
            skill = self.data.skills_storage(name).set_enabled(input_data.name, input_data.enabled)
            return {"status": "ok", "skill": skill.to_dict()}

        @app.get("/api/console/agents/{name}/messages", tags=["Console Data"])
        async def messages(name: str, count: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
            return self._rewrite_blob_payload(name, await self.data.messages(name, count=count, offset=offset))

        @app.get("/api/console/agents/{name}/messages/search", tags=["Console Data"])
        async def messages_search(name: str, query: str = Query(..., min_length=1)):
            return self._rewrite_blob_payload(name, await self.data.message_search(name, query))

        @app.get("/api/console/agents/{name}/messages/stats", tags=["Console Data"])
        async def messages_stats(name: str):
            return await self.data.message_stats(name)

        @app.post("/api/console/agents/{name}/messages/clear", tags=["Console Data"])
        async def messages_clear(name: str):
            return await self.data.clear_messages(name)

        @app.get("/api/console/agents/{name}/tasks", tags=["Console Data"])
        async def tasks(name: str):
            return self.data.tasks(name)

        @app.delete("/api/console/agents/{name}/tasks/delete", tags=["Console Data"])
        async def tasks_delete(name: str, task_id: str = Query(...)):
            return self.data.delete_task(name, task_id)

    def _add_setup_session_routes(self, app: FastAPI) -> None:
        @app.post("/api/console/agents/{name}/setup-sessions", tags=["Console Setup"])
        async def setup_session_create(name: str, payload: dict[str, Any] = Body(...)):
            return self.setup_sessions.create_session(name, payload)

        @app.get("/api/console/setup-sessions/{session_id}/events", tags=["Console Setup"])
        async def setup_session_events(session_id: str, stream: bool = Query(False)):
            session = self.setup_sessions.get_session(session_id)
            if not stream:
                return {"session_id": session.session_id, "events": session.events}

            async def event_stream():
                index = 0
                while True:
                    while index < len(session.events):
                        event = session.events[index]
                        index += 1
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if session.cancelled or (session.events and session.events[-1].get("phase") in {"done", "error", "cancelled"}):
                        break
                    await asyncio.sleep(0.5)

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        @app.post("/api/console/setup-sessions/{session_id}/cancel", tags=["Console Setup"])
        async def setup_session_cancel(session_id: str):
            return self.setup_sessions.cancel_session(session_id)

    def _add_websocket_routes(self, app: FastAPI) -> None:
        @app.websocket("/ws/console/agents/{name}/chat")
        async def console_chat(websocket: WebSocket, name: str):
            await websocket.accept()
            running, _api_url, api_ws_url = self.channels.api_is_running(name)
            if not running:
                await websocket.send_json({"type": "error", "error": "channel_required", "code": "channel_required"})
                await websocket.send_json({"type": "done"})
                await websocket.close(code=1000)
                return
            upstream_url = f"{api_ws_url}/ws/chat"
            try:
                async with websockets.connect(upstream_url) as upstream:
                    while True:
                        try:
                            raw_payload = await websocket.receive_text()
                        except WebSocketDisconnect:
                            break
                        await upstream.send(raw_payload)
                        while True:
                            raw_event = await upstream.recv()
                            parsed = json.loads(raw_event)
                            await websocket.send_json(self._rewrite_blob_payload(name, parsed))
                            if parsed.get("type") == "done":
                                break
            except WebSocketDisconnect:
                return
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": str(exc)})
                await websocket.send_json({"type": "done"})

        @app.websocket("/ws/console/agents/{name}/tasks")
        async def console_tasks(websocket: WebSocket, name: str):
            user_id = (websocket.query_params.get("user_id") or "web_user").strip() or "web_user"
            await websocket.accept()
            running, _api_url, api_ws_url = self.channels.api_is_running(name)
            if not running:
                await websocket.send_json({"type": "error", "error": "channel_required", "code": "channel_required"})
                await websocket.close(code=1000)
                return
            upstream_url = f"{api_ws_url}/ws/tasks?{urlencode({'user_id': user_id})}"
            try:
                async with websockets.connect(upstream_url) as upstream:
                    async for raw_event in upstream:
                        parsed = json.loads(raw_event)
                        await websocket.send_json(self._rewrite_blob_payload(name, parsed))
            except WebSocketDisconnect:
                return
            except Exception as exc:
                await websocket.send_json({"type": "error", "error": str(exc)})

    def _rewrite_blob_payload(self, name: str, payload: Any) -> Any:
        if isinstance(payload, dict):
            return {key: self._rewrite_blob_payload(name, value) for key, value in payload.items()}
        if isinstance(payload, list):
            return [self._rewrite_blob_payload(name, item) for item in payload]
        if isinstance(payload, str):
            return self._rewrite_blob_url(name, payload)
        return payload

    @staticmethod
    def _rewrite_blob_url(name: str, value: str) -> str:
        if not value.startswith("/api/workspace/blob"):
            return value
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        path = query.get("path", [""])[0]
        return f"/api/console/agents/{name}/workspace/blob?{urlencode({'path': path})}"

    def run(self, host: Optional[str] = None, port: Optional[int] = None, open_browser: bool = False) -> None:
        host = host if host is not None else BaseAgentConfig.DEFAULT_HOST
        port = port if port is not None else CONSOLE_PORT
        self.logger.info("Starting xAgent Console on %s:%s", host, port)

        if open_browser and self._enable_web:
            import threading
            import webbrowser

            browse_host = "127.0.0.1" if host == "0.0.0.0" else host
            if ":" in browse_host and not browse_host.startswith("["):
                browse_host = f"[{browse_host}]"
            url = f"http://{browse_host}:{port}"
            threading.Timer(1.5, lambda: webbrowser.open(url)).start()

        uvicorn.run(self.app, host=host, port=port)
