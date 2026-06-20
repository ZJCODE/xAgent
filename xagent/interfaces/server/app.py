import asyncio
import json
import logging
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from ..base import BaseAgentConfig, BaseAgentRunner
from .files import WorkspaceFileService
from .admin_routes import register_admin_routes
from .models import (
    AgentInput,
    ChatInput,
    ChatAttachmentInput,
    ChatImageInput,
    IdentityInput,
    ObserveInput,
    SkillCreateInput,
    SkillStateInput,
    SkillWriteInput,
    WorkspaceWriteInput,
)
from .runtime_routes import register_runtime_routes
from .serializers import message_item, message_search_result, response_payload
from .web import register_spa_routes
from ...components.skills import FilesystemSkillsStore
from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import (
    AsyncTaskScheduler,
    ScheduledDeliveryContext,
    create_runtime_heartbeat,
    list_active_task_views,
    scheduled_delivery_context,
)
from ...schemas.attachment import (
    MAX_ATTACHMENT_BYTES,
    MAX_MESSAGE_ATTACHMENT_BYTES,
    attachment_image_sources,
    dedupe_attachments,
)
from ...tools.image_generation_tool import normalize_image_generation_provider
from ...utils.image_utils import (
    MAX_IMAGE_BYTES,
    MAX_IMAGES_PER_MESSAGE,
    SUPPORTED_UPLOAD_IMAGE_MIME_TYPES,
    data_uri_to_bytes,
    workspace_blob_url,
)

_STATIC_DIR = Path(__file__).parent.parent / "static"
_WORKSPACE_TEXT_READ_LIMIT = 1_000_000
_WORKSPACE_SEARCH_TEXT_LIMIT = 2_000_000


@dataclass(frozen=True)
class _ScheduledTaskResult:
    content: str
    attachments: List[Dict[str, Any]] = field(default_factory=list)


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
            # Resolve config directory from agent, param, or default.
            config_dir_path = Path(
                getattr(agent, "config_dir", None) or config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR
            ).expanduser().resolve()
            self.config_dir = config_dir_path
            self.config_path = config_dir_path / BaseAgentConfig.CONFIG_FILENAME
            self.identity_path = config_dir_path / BaseAgentConfig.IDENTITY_FILENAME
            # Load config from disk if available; fall back to empty dict.
            try:
                self.config = self._load_config(self.config_path)
            except Exception:
                self.config = {}
            # Load identity from disk if available; fall back to agent attribute.
            try:
                self.identity = self._load_identity(self.identity_path)
            except Exception:
                self.identity = getattr(agent, "system_prompt", "") or getattr(agent, "identity", "")
            self.message_storage = self.agent.message_storage
            self.skills_storage = getattr(self.agent, "skills_storage", None)
            self._temporary_runtime = None
            runtime_root = getattr(self.agent, "workspace", None) or str(config_dir_path)
            if runtime_root is None:
                self._temporary_runtime = tempfile.TemporaryDirectory(prefix="xagent-runtime-")
                runtime_root = self._temporary_runtime.name
            self.workspace = Path(runtime_root).expanduser().resolve()
            self.workspace_dir = Path(getattr(self.agent, "workspace_dir", self.workspace / BaseAgentConfig.WORKSPACE_DIRNAME)).expanduser().resolve()
            self.tasks_dir = self.workspace / BaseAgentConfig.TASKS_DIRNAME
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
        else:
            super().__init__(config_dir=config_dir)

        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self._task_scheduler: Optional[AsyncTaskScheduler] = None
        self._task_subscribers: dict[str, set[WebSocket]] = {}
        self._task_subscribers_lock = asyncio.Lock()
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
        attachments = self._input_attachments(input_data)
        image_sources = self._input_image_sources(input_data, attachments=attachments)
        context = self._scheduled_delivery_context(input_data, channel="api")
        with scheduled_delivery_context(context):
            return await self.agent(
                user_message=input_data.user_message,
                user_id=input_data.user_id,
                image_source=image_sources,
                attachments=attachments,
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
            attachments = self._input_attachments(input_data)
            context = self._scheduled_delivery_context(input_data, channel="web")
            with scheduled_delivery_context(context):
                response = chat_events(
                    user_message=input_data.user_message,
                    user_id=input_data.user_id,
                    image_source=self._input_image_sources(input_data, attachments=attachments),
                    attachments=attachments,
                    stream=bool(input_data.stream),
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
                "result": response_payload(response),
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

    @staticmethod
    def _scheduled_delivery_context(input_data: ChatInput, *, channel: str) -> ScheduledDeliveryContext:
        return ScheduledDeliveryContext(
            channel=channel,
            user_id=input_data.user_id,
            target={"user_id": input_data.user_id},
            metadata={"source": channel},
        )

    def _can_handle_scheduled_task(self, task) -> bool:
        if task.kind != "task":
            return False
        target_channel = task.delivery_channel
        return target_channel in {"api", "web"}

    async def _dispatch_scheduled_task(self, task) -> None:
        result = await self._scheduled_task_result(task)
        if not result.content and not result.attachments:
            raise ValueError("scheduled task produced no content")
        metadata = {
            "scheduled_task": {
                "id": task.task_id,
                "name": task.name,
                "type": task.task_type,
                "run_at": task.run_at.isoformat(sep=" "),
                "delivery": task.delivery,
            }
        }
        stored_message = None
        if task.task_type == "message":
            message_service = getattr(self.agent, "message_service", None)
            store_model_reply = getattr(message_service, "store_model_reply", None)
            if callable(store_model_reply):
                stored_message = await store_model_reply(
                    result.content,
                    getattr(self.agent, "_assistant_sender_id", "agent"),
                    metadata=metadata,
                    attachments=result.attachments,
                )
        await self._broadcast_scheduled_message(
            task,
            result.content,
            stored_message=stored_message,
            attachments=result.attachments,
        )

    async def _scheduled_task_result(self, task) -> _ScheduledTaskResult:
        task_type = task.task_type
        if task_type == "message":
            return _ScheduledTaskResult(task.content.strip())
        if task_type != "agent":
            raise ValueError(f"unsupported scheduled task type: {task_type}")

        user_id = task.delivery_user_id or str(task.target.get("user_id") or AgentConfig.DEFAULT_USER_ID)
        prompt = AgentConfig.scheduled_agent_prompt(task.content)
        context = ScheduledDeliveryContext(
            channel=task.delivery_channel,
            user_id=user_id,
            target=task.delivery.get("target") if isinstance(task.delivery.get("target"), dict) else {},
            metadata={
                "source": "scheduled_task",
                "task_id": task.task_id,
                "task_name": task.name,
                "task_type": task.task_type,
            },
        )
        await self._acquire_chat_slot()
        try:
            deadline = time.monotonic() + self._chat_timeout
            with scheduled_delivery_context(context):
                chat_events = getattr(self.agent, "chat_events", None)
                if callable(chat_events):
                    return await self._scheduled_agent_event_result(
                        chat_events,
                        prompt=prompt,
                        user_id=user_id,
                        deadline=deadline,
                    )

                chat = getattr(self.agent, "chat", None)
                if not callable(chat):
                    raise RuntimeError("Agent does not support chat_events() or chat().")
                response = await self._await_before_deadline(
                    chat(
                        user_message=prompt,
                        user_id=user_id,
                    ),
                    deadline,
                )
        finally:
            self._chat_semaphore.release()
        return self._scheduled_response_result(response)

    async def _scheduled_agent_event_result(
        self,
        chat_events,
        *,
        prompt: str,
        user_id: str,
        deadline: float,
    ) -> _ScheduledTaskResult:
        final_content = ""
        final_attachments: List[Dict[str, Any]] = []
        last_error = ""
        async for event in self._iterate_before_deadline(
            chat_events(
                user_message=prompt,
                user_id=user_id,
                stream=False,
            ),
            deadline,
        ):
            event_type = event.get("type")
            if event_type == "message_done" and str(event.get("phase") or "final") == "final":
                final_content = str(event.get("content") or "").strip()
                raw_attachments = event.get("attachments")
                final_attachments = dedupe_attachments(raw_attachments if isinstance(raw_attachments, list) else [])
            elif event_type == "error":
                last_error = str(event.get("error") or "").strip()
        if final_content or final_attachments:
            return _ScheduledTaskResult(final_content, final_attachments)
        return _ScheduledTaskResult(last_error)

    @staticmethod
    def _scheduled_response_result(response: Any) -> _ScheduledTaskResult:
        result = response_payload(response)
        if isinstance(result, str):
            return _ScheduledTaskResult(result.strip())
        return _ScheduledTaskResult(json.dumps(result, ensure_ascii=False).strip())

    async def _broadcast_scheduled_message(
        self,
        task,
        content: str,
        *,
        stored_message=None,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        target = task.target
        user_id = str(target.get("user_id") or task.delivery_user_id or "")
        if not user_id:
            return
        normalized_attachments = dedupe_attachments(list(attachments or []))
        payload: Dict[str, Any] = {
            "type": "scheduled_message",
            "content": content,
            "task": task.to_dict(),
        }
        if normalized_attachments:
            payload["attachments"] = normalized_attachments
        if stored_message is not None:
            payload["message"] = message_item(stored_message)

        async with self._task_subscribers_lock:
            subscribers = list(self._task_subscribers.get(user_id, set()))
        stale: list[WebSocket] = []
        for websocket in subscribers:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._task_subscribers_lock:
                registered = self._task_subscribers.get(user_id)
                if registered is not None:
                    for websocket in stale:
                        registered.discard(websocket)
                    if not registered:
                        self._task_subscribers.pop(user_id, None)

    async def _register_task_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        async with self._task_subscribers_lock:
            self._task_subscribers.setdefault(user_id, set()).add(websocket)

    async def _unregister_task_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        async with self._task_subscribers_lock:
            subscribers = self._task_subscribers.get(user_id)
            if subscribers is None:
                return
            subscribers.discard(websocket)
            if not subscribers:
                self._task_subscribers.pop(user_id, None)

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
    def _input_image_sources(
        input_data: ChatInput,
        *,
        attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Union[str, List[str]]]:
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
        sources.extend(attachment_image_sources(attachments or []))
        deduped_sources: List[str] = []
        seen_sources: set[str] = set()
        for source in sources:
            normalized = str(source or "").strip()
            if normalized and normalized not in seen_sources:
                seen_sources.add(normalized)
                deduped_sources.append(normalized)
        sources = deduped_sources
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
    def _input_attachments(input_data: ChatInput) -> Optional[List[Dict[str, Any]]]:
        raw_attachments: List[Dict[str, Any]] = []
        for attachment in input_data.attachments or []:
            raw_attachments.append(attachment.model_dump(exclude_none=True))
        for image in input_data.images or []:
            raw_attachments.append({
                "kind": "image",
                "path": image.workspace_path,
                "blob_url": image.blob_url,
                "mime_type": image.mime_type,
                "file_name": image.original_name,
                "size_bytes": image.size_bytes,
                "source_channel": "web",
            })
        attachments = dedupe_attachments(raw_attachments)
        if not attachments:
            return None
        total_size = sum(int(attachment.get("size_bytes") or 0) for attachment in attachments)
        if total_size > MAX_MESSAGE_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail="Message attachments exceed 200MB")
        return attachments

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

    def _workspace_files(self) -> WorkspaceFileService:
        return WorkspaceFileService(self._get_workspace_root())

    def _get_skills_root(self) -> Path:
        skills_storage = getattr(self, "skills_storage", None)
        if skills_storage is not None:
            root = getattr(skills_storage, "root", None)
            if root is not None:
                return Path(root).expanduser().resolve()
        runtime_root = getattr(self, "workspace", None)
        if runtime_root is not None:
            skills_root = Path(runtime_root) / BaseAgentConfig.SKILLS_DIRNAME
        else:
            memory_root = self._get_memory_root()
            skills_root = memory_root.parent / BaseAgentConfig.SKILLS_DIRNAME
        root = Path(skills_root).expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _get_skills_storage(self) -> FilesystemSkillsStore:
        skills_storage = getattr(self, "skills_storage", None)
        if isinstance(skills_storage, FilesystemSkillsStore):
            return skills_storage
        storage = FilesystemSkillsStore(self._get_skills_root())
        self.skills_storage = storage
        if not hasattr(self.agent, "skills_storage"):
            try:
                self.agent.skills_storage = storage
            except Exception:
                pass
        return storage

    @staticmethod
    def _raise_skills_http_error(exc: Exception) -> None:
        if isinstance(exc, PermissionError):
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if isinstance(exc, FileNotFoundError):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise HTTPException(status_code=500, detail=f"Skills error: {str(exc)}") from exc

    @staticmethod
    def _memory_scope_roots(memory_dir: Path) -> List[Path]:
        return [memory_dir / scope for scope in ("daily", "weekly", "monthly", "yearly")]

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
            instruction_builder = getattr(self.agent, "instruction_builder", None)
            if instruction_builder is not None:
                instruction_builder.system_prompt = identity
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
        task_scheduler = AsyncTaskScheduler(
            self.tasks_dir,
            can_handle=self._can_handle_scheduled_task,
            dispatch=self._dispatch_scheduled_task,
            logger_=self.logger,
        )
        try:
            if heartbeat is not None:
                await heartbeat.start()
                self.logger.info(
                    "Runtime heartbeat started (interval=%ss)",
                    heartbeat.interval_seconds,
                )
            self._task_scheduler = task_scheduler
            await task_scheduler.start()
            self.logger.info("Scheduled task runtime started: tasks=%s", self.tasks_dir)
            yield
        finally:
            await task_scheduler.stop()
            self._task_scheduler = None
            self.logger.info("Scheduled task runtime stopped")
            if heartbeat is not None:
                await heartbeat.stop()
                self.logger.info("Runtime heartbeat stopped")

    def _add_web_ui(self, app: FastAPI) -> None:
        register_spa_routes(app, static_dir=_STATIC_DIR, logger=self.logger)

    def _add_routes(self, app: FastAPI) -> None:
        register_runtime_routes(app, self)
        register_admin_routes(
            app,
            self,
            workspace_text_limit=_WORKSPACE_TEXT_READ_LIMIT,
            workspace_search_text_limit=_WORKSPACE_SEARCH_TEXT_LIMIT,
        )

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
