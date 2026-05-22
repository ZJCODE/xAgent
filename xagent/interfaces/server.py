import asyncio
import json
import logging
import mimetypes
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from posthog import host
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, ValidationError

from .base import BaseAgentConfig, BaseAgentRunner
from ..core.agent import Agent
from ..core.config import AgentConfig
from ..core.runtime import create_runtime_heartbeat
from ..schemas import Message
from ..tools.image_generation_tool import normalize_image_generation_provider
from ..utils.image_utils import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    SUPPORTED_UPLOAD_IMAGE_MIME_TYPES,
    data_uri_to_bytes,
    detect_image_mime,
    workspace_blob_relative_path,
    workspace_blob_url,
)

_STATIC_DIR = Path(__file__).parent / "static"
_WORKSPACE_TEXT_READ_LIMIT = 1_000_000
_WORKSPACE_SEARCH_TEXT_LIMIT = 2_000_000


class ChatImageInput(BaseModel):
    """Optional image metadata accepted by API clients."""

    model_config = ConfigDict(extra="ignore")

    workspace_path: Optional[str] = None
    external_url: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    blob_url: Optional[str] = None
    original_name: Optional[str] = None


class ChatInput(BaseModel):
    """Final-only request body for the HTTP chat endpoint."""

    model_config = ConfigDict(extra="forbid")

    user_id: str
    user_message: str
    image_source: Optional[Union[str, List[str]]] = None
    images: Optional[List[ChatImageInput]] = None
    history_count: Optional[int] = AgentConfig.DEFAULT_HISTORY_COUNT
    max_iter: Optional[int] = AgentConfig.DEFAULT_MAX_ITER
    max_concurrent_tools: Optional[int] = AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS
    enable_memory: Optional[bool] = True


class AgentInput(ChatInput):
    """Event request body for WebSocket chat."""

    stream: Optional[bool] = False


class ObserveInput(BaseModel):
    """Request body for observation endpoint."""

    context: str
    source: Optional[str] = "environment"
    event_type: Optional[str] = "observation"
    metadata: Optional[Dict[str, Any]] = None


class IdentityInput(BaseModel):
    """Request body for updating identity.md."""

    identity: str


class WorkspaceWriteInput(BaseModel):
    """Request body for writing a text file in workspace/."""

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    create_parents: bool = True


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

    async def _call_agent(self, input_data: ChatInput):
        image_sources = self._input_image_sources(input_data)
        return await self.agent(
            user_message=input_data.user_message,
            user_id=input_data.user_id,
            history_count=input_data.history_count,
            max_iter=input_data.max_iter,
            max_concurrent_tools=input_data.max_concurrent_tools,
            image_source=image_sources,
            enable_memory=input_data.enable_memory,
        )

    async def _call_observe(self, input_data: ObserveInput):
        return await self.agent.observe(
            context=input_data.context,
            source=input_data.source or "environment",
            event_type=input_data.event_type or "observation",
            metadata=input_data.metadata,
        )

    async def _run_chat_with_limits(self, input_data: ChatInput):
        await self._acquire_chat_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            return await self._await_before_deadline(
                self._call_agent(input_data),
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

    async def _chat_event_stream(self, input_data: AgentInput):
        acquired = False
        done_sent = False
        try:
            await self._acquire_chat_slot()
            acquired = True
            deadline = time.monotonic() + self._chat_timeout

            chat_events = getattr(self.agent, "chat_events", None)
            if not callable(chat_events):
                raise RuntimeError("Agent does not support chat_events().")

            response = chat_events(
                user_message=input_data.user_message,
                user_id=input_data.user_id,
                history_count=input_data.history_count,
                max_iter=input_data.max_iter,
                max_concurrent_tools=input_data.max_concurrent_tools,
                image_source=self._input_image_sources(input_data),
                stream=bool(input_data.stream),
                enable_memory=input_data.enable_memory,
            )
            async for event in self._iterate_before_deadline(response, deadline):
                if event.get("type") == "done":
                    done_sent = True
                yield event
        except HTTPException as exc:
            self.logger.warning("WebSocket chat rejected for %s: %s", input_data.user_id, exc.detail)
            yield {"type": "error", "error": exc.detail, "status_code": exc.status_code}
        except asyncio.TimeoutError:
            self.logger.error("WebSocket chat timed out for %s", input_data.user_id)
            yield {"type": "error", "error": "Agent chat timed out.", "status_code": 504}
        except Exception as exc:
            self.logger.error("WebSocket chat event error for %s: %s", input_data.user_id, exc)
            yield {"type": "error", "error": str(exc)}
        finally:
            if acquired:
                self._chat_semaphore.release()
        if not done_sent:
            yield {"type": "done"}

    async def _send_websocket_chat_events(self, websocket: WebSocket, input_data: AgentInput) -> None:
        async for event in self._chat_event_stream(input_data):
            await websocket.send_json(event)

    async def _send_websocket_observe_events(self, websocket: WebSocket, input_data: ObserveInput) -> None:
        try:
            response = await self._run_observe_with_limits(input_data)
            await websocket.send_json({
                "type": "result",
                "result": self._response_payload(response),
            })
        except HTTPException as exc:
            self.logger.warning(
                "WebSocket observe rejected: source=%s type=%s detail=%s",
                input_data.source,
                input_data.event_type,
                exc.detail,
            )
            await websocket.send_json({
                "type": "error",
                "error": exc.detail,
                "status_code": exc.status_code,
            })
        except Exception as exc:
            self.logger.error(
                "WebSocket observe error: source=%s type=%s error=%s",
                input_data.source,
                input_data.event_type,
                exc,
            )
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
    def _response_payload(response):
        if hasattr(response, "model_dump"):
            return response.model_dump()
        return str(response)

    @staticmethod
    def _input_image_sources(input_data: ChatInput) -> Optional[Union[str, List[str]]]:
        sources: List[str] = []
        raw_source = input_data.image_source
        if raw_source:
            if isinstance(raw_source, list):
                sources.extend(str(item) for item in raw_source if str(item or "").strip())
            else:
                sources.append(str(raw_source))
        for image in input_data.images or []:
            source = image.blob_url or image.external_url or image.workspace_path or ""
            if source:
                sources.append(source)
        if not sources:
            return None
        if len(sources) > MAX_IMAGES_PER_MESSAGE:
            raise HTTPException(
                status_code=413,
                detail=f"At most {MAX_IMAGES_PER_MESSAGE} images are allowed per message",
            )
        for source in sources:
            if str(source).startswith("data:image/"):
                try:
                    data_uri_to_bytes(source)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
        return sources[0] if len(sources) == 1 else sources

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

    def _get_workspace_root(self) -> Path:
        workspace_dir = getattr(self, "workspace_dir", None)
        if workspace_dir is None:
            workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            runtime_root = getattr(self, "workspace", None)
            if runtime_root is not None:
                workspace_dir = Path(runtime_root) / BaseAgentConfig.WORKSPACE_DIRNAME
        if workspace_dir is None:
            memory_root = self._get_memory_root()
            workspace_dir = memory_root.parent / BaseAgentConfig.WORKSPACE_DIRNAME
        root = Path(workspace_dir).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _resolve_workspace_path(self, relative_path: str = "") -> Path:
        workspace_root = self._get_workspace_root()
        requested = (workspace_root / (relative_path or "")).expanduser().resolve()
        if not requested.is_relative_to(workspace_root):
            raise HTTPException(status_code=403, detail="Access denied")
        return requested

    @staticmethod
    def _memory_scope_roots(memory_dir: Path) -> List[Path]:
        return [memory_dir / scope for scope in ("daily", "weekly", "monthly", "yearly")]

    @staticmethod
    def _safe_child(path: Path, root: Path) -> Optional[Path]:
        try:
            resolved = path.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(root):
            return None
        return resolved

    def _workspace_metadata(self, path: Path, root: Optional[Path] = None) -> Dict[str, Any]:
        workspace_root = root or self._get_workspace_root()
        resolved = path.resolve()
        stat = resolved.stat()
        is_dir = resolved.is_dir()
        mime_type, _ = mimetypes.guess_type(resolved.name)
        return {
            "name": resolved.name,
            "path": str(resolved.relative_to(workspace_root)),
            "type": "dir" if is_dir else "file",
            "size": stat.st_size,
            "modified": stat.st_mtime,
            "mime_type": mime_type or "application/octet-stream",
            "binary": False if is_dir else self._is_binary_file(resolved),
        }

    def _scan_workspace_tree(self, directory: Path, root: Path) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        try:
            children = sorted(directory.iterdir(), key=lambda path: (not path.is_dir(), path.name.lower()))
        except (OSError, PermissionError):
            return entries

        for child in children:
            resolved = self._safe_child(child, root)
            if resolved is None:
                continue
            try:
                item = self._workspace_metadata(resolved, root)
            except OSError:
                continue
            if item["type"] == "dir" and not child.is_symlink():
                item["children"] = self._scan_workspace_tree(resolved, root)
            entries.append(item)
        return entries

    @staticmethod
    def _is_binary_file(path: Path) -> bool:
        try:
            chunk = path.read_bytes()[:4096]
        except OSError:
            return True
        if b"\0" in chunk:
            return True
        try:
            chunk.decode("utf-8")
        except UnicodeDecodeError:
            return True
        return False

    @staticmethod
    def _read_text_file(path: Path, limit: int) -> str:
        if path.stat().st_size > limit:
            raise HTTPException(status_code=413, detail="File is too large to read as text")
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=415, detail="File is not UTF-8 text") from exc

    @staticmethod
    def _message_item(message: Message) -> Dict[str, Any]:
        images = AgentHTTPServer._message_images(message)
        item = {
            "role": message.role.value if hasattr(message.role, "value") else str(message.role),
            "type": message.type.value if hasattr(message.type, "value") else str(message.type),
            "content": message.content,
            "sender_id": message.sender_id,
            "timestamp": message.timestamp,
            "metadata": message.metadata,
            "images": images,
            "image_count": len(images),
        }
        if message.tool_call:
            item["tool_call"] = {
                "name": message.tool_call.name,
                "arguments": message.tool_call.arguments,
                "output": message.tool_call.output,
            }
        return item

    @staticmethod
    def _message_images(message: Message) -> List[Dict[str, Any]]:
        metadata_images = message.metadata.get("images") if isinstance(message.metadata, dict) else None
        if isinstance(metadata_images, list):
            return [
                {key: value for key, value in dict(image).items() if value not in (None, "")}
                for image in metadata_images
                if isinstance(image, dict)
            ]
        if not message.multimodal or not message.multimodal.image:
            return []

        images = message.multimodal.image if isinstance(message.multimodal.image, list) else [message.multimodal.image]
        result: List[Dict[str, Any]] = []
        for image in images:
            source = str(getattr(image, "source", "") or "")
            if not source:
                continue
            item: Dict[str, Any] = {"mime_type": AgentHTTPServer._image_mime_type(source, getattr(image, "format", ""))}
            relative_path = workspace_blob_relative_path(source)
            if relative_path:
                item["workspace_path"] = relative_path
                item["blob_url"] = workspace_blob_url(relative_path)
            elif source.startswith(("http://", "https://")):
                item["external_url"] = source
            result.append({key: value for key, value in item.items() if value not in (None, "")})
        return result

    @staticmethod
    def _image_mime_type(source: str, image_format: str = "") -> str:
        if source.startswith("data:image/"):
            return source.split(";", 1)[0].removeprefix("data:").lower()
        normalized_format = str(image_format or "").strip().lower()
        if normalized_format == "jpeg":
            return "image/jpeg"
        if normalized_format == "webp":
            return "image/webp"
        if normalized_format == "gif":
            return "image/gif"
        return "image/png"

    @staticmethod
    def _message_search_fields(message: Message) -> List[tuple[str, str]]:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)
        message_type = message.type.value if hasattr(message.type, "value") else str(message.type)
        fields: List[tuple[str, str]] = [
            ("content", message.content or ""),
            ("sender", message.sender_id or ""),
            ("role", role),
            ("type", message_type),
        ]

        if message.tool_call:
            tool_parts = [
                str(message.tool_call.name or ""),
                str(message.tool_call.arguments or ""),
                str(message.tool_call.output or ""),
            ]
            tool_text = " ".join(part for part in tool_parts if part)
            if tool_text:
                fields.append(("tool", tool_text))

        if message.metadata:
            metadata_text = json.dumps(message.metadata, ensure_ascii=False, sort_keys=True, default=str)
            fields.append(("metadata", metadata_text))

        return fields

    @staticmethod
    def _build_search_snippet(text: str, query: str) -> str:
        if not text:
            return ""

        normalized_query = query.strip().lower()
        lower_text = text.lower()
        match_index = lower_text.find(normalized_query)
        if match_index == -1:
            return text[:200].replace("\n", " ").strip()

        start = max(0, match_index - 80)
        end = min(len(text), match_index + len(query) + 120)
        return text[start:end].replace("\n", " ").strip()

    def _message_search_result(self, message: Message, query: str) -> Optional[Dict[str, Any]]:
        normalized_query = query.strip().lower()
        if not normalized_query:
            return None

        matched_in: List[str] = []
        snippet = ""
        for field, text in self._message_search_fields(message):
            if not text:
                continue
            if normalized_query not in text.lower():
                continue
            matched_in.append(field)
            if not snippet:
                snippet = self._build_search_snippet(text, query)

        if not matched_in:
            return None

        return {
            **self._message_item(message),
            "matched_in": matched_in,
            "snippet": snippet,
        }

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
            description="HTTP and WebSocket API for xAgent",
            version="1.0.0",
            lifespan=self._lifespan,
        )
        self._add_routes(app)
        if self._enable_web:
            self._add_web_ui(app)
        return app

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        heartbeat = create_runtime_heartbeat(
            self.agent,
            self.config.get("runtime") if isinstance(self.config, dict) else None,
            logger_=self.logger,
        )
        try:
            if heartbeat is not None:
                await heartbeat.start()
                self.logger.info(
                    "Runtime heartbeat started (interval=%ss)",
                    heartbeat.interval_seconds,
                )
            yield
        finally:
            if heartbeat is not None:
                await heartbeat.stop()
                self.logger.info("Runtime heartbeat stopped")
            await self._flush_agent_memory()

    async def _flush_agent_memory(self) -> None:
        flusher = getattr(self.agent, "flush_memory", None)
        if flusher is not None:
            await flusher()

    def _add_web_ui(self, app: FastAPI) -> None:
        if _STATIC_DIR.is_dir():
            async def serve_spa_index():
                index = _STATIC_DIR / "index.html"
                if index.exists():
                    return FileResponse(str(index), media_type="text/html")
                raise HTTPException(status_code=404, detail="Web UI not found")

            @app.get("/", include_in_schema=False)
            async def serve_index():
                return await serve_spa_index()

            @app.get("/memory", include_in_schema=False)
            async def serve_memory():
                return await serve_spa_index()

            @app.get("/workspace", include_in_schema=False)
            async def serve_workspace():
                return await serve_spa_index()

            @app.get("/message", include_in_schema=False)
            async def serve_message():
                return await serve_spa_index()

            @app.get("/agent", include_in_schema=False)
            async def serve_agent():
                return await serve_spa_index()

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
        async def chat(input_data: ChatInput):
            self.logger.info(
                "Chat request from %s",
                input_data.user_id,
            )
            try:
                response = await self._run_chat_with_limits(input_data)
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
                        "WebSocket observe request: source=%s, type=%s",
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
                "Observation request: source=%s, type=%s",
                input_data.source,
                input_data.event_type,
            )
            try:
                response = await self._run_observe_with_limits(input_data)
                return self._response_payload(response)
            except HTTPException:
                raise
            except Exception as exc:
                self.logger.error(
                    "Agent observe error: source=%s type=%s error=%s",
                    input_data.source,
                    input_data.event_type,
                    exc,
                )
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
            provider_cfg = self.config.get("provider") if isinstance(self.config, dict) else {}
            provider_name = provider_cfg.get("name") if isinstance(provider_cfg, dict) else None
            image_generation_cfg = self.config.get("image_generation") if isinstance(self.config, dict) else {}
            image_generation_provider = "none"
            if isinstance(image_generation_cfg, dict):
                try:
                    image_generation_provider = normalize_image_generation_provider(image_generation_cfg.get("provider"))
                except ValueError:
                    image_generation_provider = str(image_generation_cfg.get("provider") or "none")
            tool_names = list(self.agent.tools.keys())
            supports_vision = bool(getattr(self.agent, "supports_vision", True))
            return {
                "provider": provider_name or "",
                "model": self.agent.model,
                "workspace": str(getattr(self, "workspace", "")),
                "workspace_dir": str(self._get_workspace_root()),
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

            tree: List[Dict[str, Any]] = []
            for scope_root in self._memory_scope_roots(memory_dir):
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

            memory_files: List[Path] = []
            for scope_root in self._memory_scope_roots(memory_dir):
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
            """Return the workspace directory tree as JSON."""
            workspace_root = self._get_workspace_root()
            return {
                "root": str(workspace_root),
                "tree": self._scan_workspace_tree(workspace_root, workspace_root),
            }

        @app.get("/api/workspace/read", tags=["Monitoring"])
        async def workspace_read(path: str = Query(..., description="Relative path inside workspace directory")):
            """Read a workspace file as UTF-8 text or return binary metadata."""
            workspace_root = self._get_workspace_root()
            requested = self._resolve_workspace_path(path)
            if not requested.is_file():
                raise HTTPException(status_code=404, detail="File not found")

            metadata = self._workspace_metadata(requested, workspace_root)
            if metadata["binary"]:
                return {**metadata, "content": "", "text": False, "blob_url": workspace_blob_url(path)}
            content = self._read_text_file(requested, _WORKSPACE_TEXT_READ_LIMIT)
            return {**metadata, "content": content, "text": True, "blob_url": workspace_blob_url(path)}

        @app.get("/api/workspace/blob", tags=["Monitoring"])
        async def workspace_blob(path: str = Query(..., description="Relative path inside workspace directory")):
            """Serve a workspace file as a binary response."""
            requested = self._resolve_workspace_path(path)
            if not requested.is_file():
                raise HTTPException(status_code=404, detail="File not found")
            mime_type, _ = mimetypes.guess_type(requested.name)
            return FileResponse(str(requested), media_type=mime_type or "application/octet-stream", filename=requested.name)

        @app.get("/api/workspace/search", tags=["Monitoring"])
        async def workspace_search(
            query: str = Query(..., min_length=1, description="Search text for workspace file names or file content"),
            limit: int = Query(50, ge=1, le=200, description="Maximum number of results to return"),
        ):
            """Search workspace files by path/name and text content."""
            workspace_root = self._get_workspace_root()
            needle = query.strip().lower()
            results: List[Dict[str, Any]] = []

            for file_path in sorted(workspace_root.rglob("*")):
                if len(results) >= limit:
                    break
                resolved = self._safe_child(file_path, workspace_root)
                if resolved is None or not resolved.is_file():
                    continue
                relative_path = str(resolved.relative_to(workspace_root))
                match_kind: List[str] = []
                snippet = ""

                if needle in resolved.name.lower() or needle in relative_path.lower():
                    match_kind.append("filename")

                is_binary = self._is_binary_file(resolved)
                if not is_binary and resolved.stat().st_size <= _WORKSPACE_SEARCH_TEXT_LIMIT:
                    try:
                        content = resolved.read_text(encoding="utf-8")
                    except (OSError, UnicodeDecodeError):
                        content = ""
                    lower_content = content.lower()
                    content_index = lower_content.find(needle)
                    if content_index != -1:
                        match_kind.append("content")
                        start = max(0, content_index - 80)
                        end = min(len(content), content_index + len(query) + 120)
                        snippet = content[start:end].replace("\n", " ").strip()

                if match_kind:
                    metadata = self._workspace_metadata(resolved, workspace_root)
                    results.append({
                        **metadata,
                        "matched_in": match_kind,
                        "snippet": snippet,
                    })

            return {"query": query, "results": results}

        @app.post("/api/workspace/clear", tags=["Monitoring"])
        async def workspace_clear():
            """Delete all files and directories inside workspace/ without deleting the root."""
            workspace_root = self._get_workspace_root()
            deleted_count = 0
            try:
                for child in workspace_root.iterdir():
                    if child.is_symlink() or child.is_file():
                        child.unlink()
                    elif child.is_dir():
                        resolved = self._safe_child(child, workspace_root)
                        if resolved is None:
                            continue
                        shutil.rmtree(resolved)
                    else:
                        child.unlink(missing_ok=True)
                    deleted_count += 1
            except Exception as exc:
                self.logger.error("Failed to clear workspace: %s", exc)
                raise HTTPException(status_code=500, detail=f"Failed to clear workspace: {str(exc)}")
            return {
                "status": "ok",
                "message": "Workspace cleared",
                "deleted": deleted_count,
            }

        @app.put("/api/workspace/write", tags=["Monitoring"])
        async def workspace_write(input_data: WorkspaceWriteInput):
            """Write a UTF-8 text file in workspace/."""
            workspace_root = self._get_workspace_root()
            requested = self._resolve_workspace_path(input_data.path)
            if requested.exists() and requested.is_dir():
                raise HTTPException(status_code=400, detail="Path is a directory")
            if input_data.create_parents:
                requested.parent.mkdir(parents=True, exist_ok=True)
            elif not requested.parent.is_dir():
                raise HTTPException(status_code=404, detail="Parent directory not found")
            requested.write_text(input_data.content, encoding="utf-8")
            return {"status": "ok", **self._workspace_metadata(requested, workspace_root)}

        @app.delete("/api/workspace/delete", tags=["Monitoring"])
        async def workspace_delete(
            path: str = Query(..., description="Relative path inside workspace directory"),
            recursive: bool = Query(False, description="Allow deleting non-empty directories"),
        ):
            """Delete a workspace file or directory."""
            workspace_root = self._get_workspace_root()
            requested = self._resolve_workspace_path(path)
            if requested == workspace_root:
                raise HTTPException(status_code=400, detail="Cannot delete workspace root")
            if not requested.exists():
                raise HTTPException(status_code=404, detail="Path not found")
            metadata = self._workspace_metadata(requested, workspace_root)
            if requested.is_dir():
                if recursive:
                    shutil.rmtree(requested)
                else:
                    requested.rmdir()
            else:
                requested.unlink()
            return {"status": "ok", "deleted": metadata}

        @app.post("/api/workspace/upload", tags=["Monitoring"])
        async def workspace_upload(
            file: UploadFile = File(...),
            path: str = Form("", description="Optional relative target path or directory inside workspace"),
        ):
            """Upload a file into workspace/."""
            workspace_root = self._get_workspace_root()
            raw_target = path.strip()
            target_is_directory = raw_target.endswith("/")
            target_relative = raw_target.strip("/")
            filename = Path(file.filename or "upload.bin").name
            if not target_relative:
                requested = self._resolve_workspace_path(filename)
            else:
                target = self._resolve_workspace_path(target_relative)
                requested = target / filename if target_is_directory or target.is_dir() else target
                requested = requested.resolve()
                if not requested.is_relative_to(workspace_root):
                    raise HTTPException(status_code=403, detail="Access denied")
            requested.parent.mkdir(parents=True, exist_ok=True)
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
            requested.write_bytes(content)
            return {"status": "ok", **self._workspace_metadata(requested, workspace_root)}

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
            items = [self._message_item(msg) for msg in messages]
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
            """Search stored messages in newest-first order."""
            total = await self.message_storage.get_message_count()
            if total <= 0 or not hasattr(self.message_storage, "get_messages"):
                return {"query": query, "results": []}

            messages = await self.message_storage.get_messages(count=total, offset=0)
            results: List[Dict[str, Any]] = []
            for message in reversed(messages):
                match = self._message_search_result(message, query)
                if match is None:
                    continue
                results.append(match)
                if len(results) >= limit:
                    break

            return {"query": query, "results": results}

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
