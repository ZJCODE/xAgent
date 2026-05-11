import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from posthog import host
import uvicorn
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from .base import BaseAgentConfig, BaseAgentRunner
from ..core.agent import Agent
from ..core.config import AgentConfig

_STATIC_DIR = Path(__file__).parent / "static"


class AgentInput(BaseModel):
    """Request body for chat endpoint."""

    user_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    stream: Optional[bool] = False
    history_count: Optional[int] = AgentConfig.DEFAULT_HISTORY_COUNT
    max_iter: Optional[int] = AgentConfig.DEFAULT_MAX_ITER
    max_concurrent_tools: Optional[int] = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
    enable_memory: Optional[bool] = True
    private: Optional[bool] = False


class ObserveInput(BaseModel):
    """Request body for observation endpoint."""

    context: str
    current_user_id: Optional[str] = AgentConfig.DEFAULT_USER_ID
    source: Optional[str] = "environment"
    event_type: Optional[str] = "observation"
    sender_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    history_count: Optional[int] = AgentConfig.DEFAULT_HISTORY_COUNT
    max_iter: Optional[int] = AgentConfig.DEFAULT_MAX_ITER
    max_concurrent_tools: Optional[int] = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
    enable_memory: Optional[bool] = True
    private: Optional[bool] = False


class IdentityInput(BaseModel):
    """Request body for updating identity.md."""

    identity: str


class AgentHTTPServer(BaseAgentRunner):
    """HTTP server for xAgent."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        agent: Optional[Agent] = None,
        enable_web: bool = True,
        max_concurrent_chats: int = AgentConfig.DEFAULT_HTTP_MAX_CONCURRENT_CHATS,
        chat_queue_timeout: float = AgentConfig.DEFAULT_HTTP_QUEUE_TIMEOUT,
        chat_timeout: float = AgentConfig.DEFAULT_HTTP_CHAT_TIMEOUT,
    ):
        self._enable_web = enable_web
        self._chat_semaphore = asyncio.Semaphore(max(1, int(max_concurrent_chats)))
        self._chat_queue_timeout = max(0.001, float(chat_queue_timeout))
        self._chat_timeout = max(0.001, float(chat_timeout))

        if agent is not None:
            self.agent = agent
            self.config = {}
            self.message_storage = self.agent.message_storage
        else:
            super().__init__(config_dir=config_dir)

        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.app = self._create_app()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def _acquire_chat_slot(self) -> None:
        try:
            await asyncio.wait_for(
                self._chat_semaphore.acquire(),
                timeout=self._chat_queue_timeout,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=429,
                detail="Too many concurrent chat requests; try again later.",
            ) from exc

    async def _call_agent(self, input_data: AgentInput, stream: bool):
        return await self.agent(
            user_message=input_data.user_message,
            user_id=input_data.user_id,
            history_count=input_data.history_count,
            max_iter=input_data.max_iter,
            max_concurrent_tools=input_data.max_concurrent_tools,
            image_source=input_data.image_source,
            stream=stream,
            enable_memory=input_data.enable_memory,
            private=input_data.private,
        )

    async def _call_observe(self, input_data: ObserveInput):
        return await self.agent.observe(
            context=input_data.context,
            current_user_id=input_data.current_user_id or AgentConfig.DEFAULT_USER_ID,
            source=input_data.source or "environment",
            event_type=input_data.event_type or "observation",
            sender_id=input_data.sender_id,
            metadata=input_data.metadata,
            history_count=input_data.history_count or AgentConfig.DEFAULT_HISTORY_COUNT,
            max_iter=input_data.max_iter or AgentConfig.DEFAULT_MAX_ITER,
            max_concurrent_tools=input_data.max_concurrent_tools or AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
            enable_memory=True if input_data.enable_memory is None else input_data.enable_memory,
            private=bool(input_data.private),
        )

    async def _run_chat_with_limits(self, input_data: AgentInput, stream: bool):
        await self._acquire_chat_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(
                self._call_agent(input_data, stream=stream),
                deadline,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent chat timed out.") from exc
        finally:
            self._chat_semaphore.release()

    async def _run_observe_with_limits(self, input_data: ObserveInput):
        await self._acquire_chat_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(
                self._call_observe(input_data),
                deadline,
            )
        except asyncio.TimeoutError as exc:
            raise HTTPException(status_code=504, detail="Agent observe timed out.") from exc
        finally:
            self._chat_semaphore.release()

    async def _chat_stream_events(self, input_data: AgentInput):
        acquired = False
        try:
            await self._acquire_chat_slot()
            acquired = True
            deadline = time.monotonic() + self._chat_timeout
            response = await self._await_before_deadline(
                self._call_agent(input_data, stream=True),
                deadline,
            )
            if hasattr(response, "__aiter__"):
                async for delta in self._iterate_before_deadline(response, deadline):
                    yield {"type": "delta", "delta": delta}
            else:
                yield {"type": "message", "message": self._response_payload(response)}
        except HTTPException as exc:
            self.logger.warning("Streaming chat rejected for %s: %s", input_data.user_id, exc.detail)
            yield {"type": "error", "error": exc.detail, "status_code": exc.status_code}
        except asyncio.TimeoutError:
            self.logger.error("Streaming chat timed out for %s", input_data.user_id)
            yield {"type": "error", "error": "Agent chat timed out.", "status_code": 504}
        except Exception as exc:
            self.logger.error("Streaming error for %s: %s", input_data.user_id, exc)
            yield {"type": "error", "error": str(exc)}
        finally:
            if acquired:
                self._chat_semaphore.release()
        yield {"type": "done"}

    async def _stream_chat_events(self, input_data: AgentInput):
        async for event in self._chat_stream_events(input_data):
            if event.get("type") == "done":
                yield "data: [DONE]\n\n"
                continue
            yield self._sse(self._sse_event_payload(event))

    async def _send_websocket_chat_events(self, websocket: WebSocket, input_data: AgentInput) -> None:
        if input_data.stream:
            async for event in self._chat_stream_events(input_data):
                await websocket.send_json(event)
            return

        try:
            response = await self._run_chat_with_limits(input_data, stream=False)
            await websocket.send_json({
                "type": "message",
                "message": self._response_payload(response),
            })
        except HTTPException as exc:
            self.logger.warning("WebSocket chat rejected for %s: %s", input_data.user_id, exc.detail)
            await websocket.send_json({
                "type": "error",
                "error": exc.detail,
                "status_code": exc.status_code,
            })
        except Exception as exc:
            self.logger.error("WebSocket chat error for %s: %s", input_data.user_id, exc)
            await websocket.send_json({
                "type": "error",
                "error": f"Agent processing error: {str(exc)}",
            })
        finally:
            await websocket.send_json({"type": "done"})

    async def _send_websocket_observe_events(self, websocket: WebSocket, input_data: ObserveInput) -> None:
        try:
            response = await self._run_observe_with_limits(input_data)
            await websocket.send_json({
                "type": "result",
                "result": self._response_payload(response),
            })
        except HTTPException as exc:
            self.logger.warning("WebSocket observe rejected for %s: %s", input_data.current_user_id, exc.detail)
            await websocket.send_json({
                "type": "error",
                "error": exc.detail,
                "status_code": exc.status_code,
            })
        except Exception as exc:
            self.logger.error("WebSocket observe error for %s: %s", input_data.current_user_id, exc)
            await websocket.send_json({
                "type": "error",
                "error": f"Agent observe error: {str(exc)}",
            })
        finally:
            await websocket.send_json({"type": "done"})

    async def _send_websocket_error(
        self,
        websocket: WebSocket,
        error: str,
        *,
        status_code: Optional[int] = None,
        details: Optional[Any] = None,
    ) -> None:
        payload: Dict[str, Any] = {"type": "error", "error": error}
        if status_code is not None:
            payload["status_code"] = status_code
        if details is not None:
            payload["details"] = details
        await websocket.send_json(payload)
        await websocket.send_json({"type": "done"})

    async def _await_before_deadline(self, awaitable, deadline: float):
        return await asyncio.wait_for(awaitable, timeout=self._remaining_time(deadline))

    async def _iterate_before_deadline(self, response, deadline: float):
        iterator = response.__aiter__()
        while True:
            try:
                yield await asyncio.wait_for(
                    iterator.__anext__(),
                    timeout=self._remaining_time(deadline),
                )
            except StopAsyncIteration:
                break

    @staticmethod
    def _sse(payload: dict) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    @staticmethod
    def _sse_event_payload(event: dict) -> dict:
        return {key: value for key, value in event.items() if key != "type"}

    @staticmethod
    def _response_payload(response):
        if hasattr(response, "model_dump"):
            return response.model_dump()
        return str(response)

    @staticmethod
    def _remaining_time(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise asyncio.TimeoutError
        return remaining

    def _get_memory_root(self) -> Path:
        memory = self.agent.markdown_memory
        memory_root = getattr(memory, "root", None)
        if memory_root is None:
            raise HTTPException(status_code=500, detail="Memory storage path is unavailable")
        return Path(memory_root).expanduser().resolve()

    def _get_identity_path(self) -> Path:
        identity_path = getattr(self, "identity_path", None)
        if identity_path is None:
            raise HTTPException(status_code=500, detail="Identity file path is unavailable")
        return Path(identity_path).expanduser().resolve()

    def _get_agent_identity(self) -> str:
        identity = getattr(self.agent, "identity", None)
        if identity is None:
            identity = getattr(self.agent, "system_prompt", "")
        return identity or ""

    def _set_agent_identity(self, identity: str) -> None:
        if hasattr(self.agent, "set_identity"):
            self.agent.set_identity(identity)
        else:
            self.agent.system_prompt = identity
            message_handler = getattr(self.agent, "message_handler", None)
            if message_handler is not None:
                message_handler.system_prompt = identity
        self.identity = identity

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent HTTP Agent Server",
            description="HTTP API for xAgent continuous-stream AI",
            version="1.0.0",
        )
        self._add_routes(app)
        if self._enable_web:
            self._add_web_ui(app)
        return app

    def _add_web_ui(self, app: FastAPI) -> None:
        if _STATIC_DIR.is_dir():
            @app.get("/", include_in_schema=False)
            async def serve_index():
                index = _STATIC_DIR / "index.html"
                if index.exists():
                    return FileResponse(str(index), media_type="text/html")
                raise HTTPException(status_code=404, detail="Web UI not found")

            @app.get("/memory", include_in_schema=False)
            async def serve_memory():
                page = _STATIC_DIR / "memory.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Memory page not found")

            @app.get("/message", include_in_schema=False)
            async def serve_message():
                page = _STATIC_DIR / "message.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Message page not found")

            @app.get("/group", include_in_schema=False)
            async def serve_group():
                page = _STATIC_DIR / "group.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Group page not found")

            @app.get("/agent", include_in_schema=False)
            async def serve_agent():
                page = _STATIC_DIR / "agent.html"
                if page.exists():
                    return FileResponse(str(page), media_type="text/html")
                raise HTTPException(status_code=404, detail="Agent page not found")

            app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
            self.logger.info("Web UI available at /")
        else:
            self.logger.warning("Static directory not found at %s — web UI disabled", _STATIC_DIR)

    def _add_routes(self, app: FastAPI) -> None:
        @app.get("/i/health", tags=["Health"])
        async def health_check():
            return "ok"

        @app.get("/health")
        async def health():
            return {"status": "healthy", "service": "xAgent HTTP Server"}

        @app.post("/chat")
        async def chat(input_data: AgentInput):
            self.logger.info(
                "Chat request from %s, stream=%s",
                input_data.user_id,
                input_data.stream,
            )
            try:
                if input_data.stream:
                    return StreamingResponse(
                        self._stream_chat_events(input_data),
                        media_type="text/event-stream",
                    )

                response = await self._run_chat_with_limits(input_data, stream=False)
                return {"reply": self._response_payload(response)}
            except HTTPException:
                raise
            except Exception as exc:
                self.logger.error("Agent processing error for %s: %s", input_data.user_id, exc)
                raise HTTPException(status_code=500, detail=f"Agent processing error: {str(exc)}")

        @app.websocket("/ws/chat")
        async def websocket_chat(websocket: WebSocket):
            await websocket.accept()
            self.logger.info("WebSocket chat connected")

            while True:
                try:
                    raw_payload = await websocket.receive_json()
                    input_data = AgentInput.model_validate(raw_payload)
                    self.logger.info(
                        "WebSocket chat request from %s, stream=%s",
                        input_data.user_id,
                        input_data.stream,
                    )
                    await self._send_websocket_chat_events(websocket, input_data)
                except WebSocketDisconnect:
                    self.logger.info("WebSocket chat disconnected")
                    break
                except json.JSONDecodeError as exc:
                    self.logger.warning("Invalid WebSocket chat JSON: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid JSON payload.",
                        status_code=400,
                        details=str(exc),
                    )
                except ValidationError as exc:
                    self.logger.warning("Invalid WebSocket chat payload: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid chat payload.",
                        status_code=422,
                        details=exc.errors(),
                    )
                except Exception as exc:
                    self.logger.error("Unexpected WebSocket chat error: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        f"Agent processing error: {str(exc)}",
                    )

        @app.websocket("/ws/observe")
        async def websocket_observe(websocket: WebSocket):
            await websocket.accept()
            self.logger.info("WebSocket observe connected")

            while True:
                try:
                    raw_payload = await websocket.receive_json()
                    input_data = ObserveInput.model_validate(raw_payload)
                    self.logger.info(
                        "WebSocket observe request from %s, source=%s, type=%s",
                        input_data.current_user_id,
                        input_data.source,
                        input_data.event_type,
                    )
                    await self._send_websocket_observe_events(websocket, input_data)
                except WebSocketDisconnect:
                    self.logger.info("WebSocket observe disconnected")
                    break
                except json.JSONDecodeError as exc:
                    self.logger.warning("Invalid WebSocket observe JSON: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid JSON payload.",
                        status_code=400,
                        details=str(exc),
                    )
                except ValidationError as exc:
                    self.logger.warning("Invalid WebSocket observe payload: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        "Invalid observe payload.",
                        status_code=422,
                        details=exc.errors(),
                    )
                except Exception as exc:
                    self.logger.error("Unexpected WebSocket observe error: %s", exc)
                    await self._send_websocket_error(
                        websocket,
                        f"Agent observe error: {str(exc)}",
                    )

        @app.post("/observe")
        async def observe(input_data: ObserveInput):
            self.logger.info(
                "Observation request from %s, source=%s, type=%s",
                input_data.current_user_id,
                input_data.source,
                input_data.event_type,
            )
            try:
                response = await self._run_observe_with_limits(input_data)
                return self._response_payload(response)
            except HTTPException:
                raise
            except Exception as exc:
                self.logger.error("Agent observe error for %s: %s", input_data.current_user_id, exc)
                raise HTTPException(status_code=500, detail=f"Agent observe error: {str(exc)}")

        @app.post("/clear_messages")
        async def clear_messages():
            self.logger.info("Clear messages request")
            try:
                await self.message_storage.clear_messages()
                return {
                    "status": "success",
                    "message": "Message stream cleared",
                }
            except Exception as exc:
                self.logger.error("Failed to clear messages: %s", exc)
                raise HTTPException(status_code=500, detail=f"Failed to clear messages: {str(exc)}")

        # ── Monitoring API endpoints ─────────────────────────────────

        @app.get("/api/agent/info", tags=["Monitoring"])
        async def agent_info():
            """Return agent metadata for monitoring pages."""
            memory_dir = str(self._get_memory_root())
            storage_info = self.message_storage.get_stream_info() if hasattr(self.message_storage, "get_stream_info") else {}
            identity = self._get_agent_identity()
            try:
                identity_path = self._get_identity_path()
                identity_path_value = str(identity_path)
                identity_editable = True
            except HTTPException:
                identity_path_value = ""
                identity_editable = False
            return {
                "model": self.agent.model,
                "workspace": str(getattr(self, "workspace", "")),
                "memory_dir": memory_dir,
                "message_storage": storage_info,
                "tools": list(self.agent.tools.keys()),
                "identity": identity,
                "identity_file": BaseAgentConfig.IDENTITY_FILENAME,
                "identity_path": identity_path_value,
                "identity_editable": identity_editable,
                "system_prompt": identity,
            }

        @app.get("/api/agent/identity", tags=["Monitoring"])
        async def agent_identity():
            """Return identity.md content."""
            identity_path = self._get_identity_path()
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
            """Persist identity.md and update the running agent."""
            identity = input_data.identity.strip()
            if not identity:
                raise HTTPException(status_code=400, detail="Identity cannot be empty")

            identity_path = self._get_identity_path()
            identity_path.parent.mkdir(parents=True, exist_ok=True)
            file_content = f"{identity}\n"
            identity_path.write_text(file_content, encoding="utf-8")
            self._set_agent_identity(identity)

            return {
                "status": "ok",
                "identity": file_content,
                "path": str(identity_path),
                "filename": identity_path.name,
                "modified": identity_path.stat().st_mtime,
            }

        @app.get("/api/memory/tree", tags=["Monitoring"])
        async def memory_tree():
            """Return the memory directory tree as JSON."""
            memory_dir = self._get_memory_root()
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

            return {"tree": _scan(memory_dir, memory_dir)}

        @app.get("/api/memory/read", tags=["Monitoring"])
        async def memory_read(path: str = Query(..., description="Relative path inside memory directory")):
            """Read a specific memory markdown file."""
            memory_dir = self._get_memory_root()
            requested = (memory_dir / path).resolve()

            # Path traversal protection
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
            """Search memory files by file name and content."""
            memory_dir = self._get_memory_root()
            needle = query.strip().lower()
            results: List[Dict[str, Any]] = []

            for file_path in sorted(memory_dir.rglob("*.md")):
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

        @app.get("/api/messages", tags=["Monitoring"])
        async def get_messages(
            count: int = Query(50, ge=1, le=500, description="Number of messages to retrieve"),
            offset: int = Query(0, ge=0, description="Number of recent messages to skip"),
        ):
            """Paginated message retrieval for the monitoring page.

            Returns messages in newest-first order so the UI can append older
            pages at the end without reordering previously rendered items.
            """
            total = await self.message_storage.get_message_count()
            messages = await self.message_storage.get_messages(count=count, offset=offset)
            items = []
            for msg in messages:
                item = {
                    "role": msg.role.value if hasattr(msg.role, "value") else str(msg.role),
                    "type": msg.type.value if hasattr(msg.type, "value") else str(msg.type),
                    "content": msg.content,
                    "sender_id": msg.sender_id,
                    "timestamp": msg.timestamp,
                    "metadata": msg.metadata,
                }
                if msg.tool_call:
                    item["tool_call"] = {
                        "name": msg.tool_call.name,
                        "arguments": msg.tool_call.arguments,
                        "output": msg.tool_call.output,
                    }
                items.append(item)
            items.reverse()
            return {
                "messages": items,
                "total": total,
                "count": count,
                "offset": offset,
                "has_more": offset + count < total,
            }

        @app.post("/api/memory/clear", tags=["Monitoring"])
        async def memory_clear():
            """Delete the entire memory directory and recreate it empty."""
            import shutil
            memory_dir = self._get_memory_root()
            try:
                shutil.rmtree(memory_dir)
            except OSError:
                pass
            memory_dir.mkdir(parents=True, exist_ok=True)
            return {"status": "ok"}

        @app.get("/api/messages/stats", tags=["Monitoring"])
        async def messages_stats():
            """Return message storage statistics."""
            total = await self.message_storage.get_message_count()
            storage_info = self.message_storage.get_stream_info() if hasattr(self.message_storage, "get_stream_info") else {}
            result: Dict[str, Any] = {"total": total, "storage": storage_info}
            if total > 0:
                oldest = await self.message_storage.get_messages(count=1, offset=total - 1)
                newest = await self.message_storage.get_messages(count=1, offset=0)
                if oldest:
                    result["earliest_timestamp"] = oldest[0].timestamp
                if newest:
                    result["latest_timestamp"] = newest[0].timestamp
            return result

    def run(self, host: str = None, port: int = None, open_browser: bool = False) -> None:
        host = host if host is not None else BaseAgentConfig.DEFAULT_HOST
        port = port if port is not None else BaseAgentConfig.DEFAULT_PORT

        self.logger.info("Starting xAgent HTTP Server on %s:%s", host, port)
        self.logger.info("Model: %s", self.agent.model)
        self.logger.info("Tools: %d loaded", len(self.agent.tools))
        self.logger.info("Web UI: %s", "enabled at /" if self._enable_web else "disabled (--no-web)")

        if open_browser and self._enable_web:
            import threading
            import webbrowser

            browse_host = host

            # 0.0.0.0 不能直接用于浏览器访问
            if browse_host == "0.0.0.0":
                browse_host = "127.0.0.1"

            # IPv6 浏览器 URL 需要 []
            if ":" in browse_host and not browse_host.startswith("["):
                browse_host = f"[{browse_host}]"

            url = f"http://{browse_host}:{port}"

            threading.Timer(1.5, lambda: webbrowser.open(url)).start()

        uvicorn.run(self.app, host=host, port=port)


_server_instance = None


def get_app() -> FastAPI:
    global _server_instance
    if _server_instance is None:
        _server_instance = AgentHTTPServer()
    return _server_instance.app


def get_app_lazy() -> FastAPI:
    return get_app()


app = None
