import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from ..base import BaseAgentConfig, BaseAgentRunner
from .files import WorkspaceFileService
from .admin_routes import register_admin_routes
from .models import ChatInput
from ...components.skills import SkillsStorageLocal
from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import (
    SubconsciousDelivery,
    create_runtime_heartbeat,
    resolve_contacts_path,
)
from ...integrations.api import ApiChannelAdapter, ChatLimits, input_attachments, input_image_sources

_WORKSPACE_TEXT_READ_LIMIT = 1_000_000
_WORKSPACE_SEARCH_TEXT_LIMIT = 2_000_000


class AgentHTTPServer(BaseAgentRunner):
    """HTTP server for the api transport channel."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        agent: Optional[Agent] = None,
        max_concurrent_chats: int = AgentConfig.DEFAULT_HTTP_MAX_CONCURRENT_CHATS,
        chat_queue_timeout: float = AgentConfig.DEFAULT_HTTP_QUEUE_TIMEOUT,
        chat_timeout: float = AgentConfig.DEFAULT_HTTP_CHAT_TIMEOUT,
    ):
        if agent is not None:
            self.agent = agent
            config_dir_path = Path(
                getattr(agent, "config_dir", None) or config_dir or BaseAgentConfig.DEFAULT_CONFIG_DIR
            ).expanduser().resolve()
            self.config_dir = config_dir_path
            self.config_path = config_dir_path / BaseAgentConfig.CONFIG_FILENAME
            self.identity_path = config_dir_path / BaseAgentConfig.IDENTITY_FILENAME
            try:
                self.config = self._load_config(self.config_path)
            except Exception:
                self.config = {}
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
            self.workspace_dir = Path(
                getattr(self.agent, "workspace_dir", self.workspace / BaseAgentConfig.WORKSPACE_DIRNAME)
            ).expanduser().resolve()
            self.tasks_dir = self.workspace / BaseAgentConfig.TASKS_DIRNAME
            self.tasks_dir.mkdir(parents=True, exist_ok=True)
        else:
            super().__init__(config_dir=config_dir)

        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        contacts_file = resolve_contacts_path(self.workspace)
        self.api = ApiChannelAdapter(
            self.agent,
            contacts_file=contacts_file,
            tasks_dir=self.tasks_dir,
            limits=ChatLimits(
                max_concurrent_chats=max_concurrent_chats,
                chat_queue_timeout=chat_queue_timeout,
                chat_timeout=chat_timeout,
            ),
            logger=self.logger,
        )
        self.app = self._create_app()
        self.app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        await self.api.deliver_subconscious_message(delivery)

    async def _register_task_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        await self.api.delivery.register_subscriber(user_id, websocket)

    async def _unregister_task_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        await self.api.delivery.unregister_subscriber(user_id, websocket)

    async def _dispatch_scheduled_task(self, task) -> None:
        await self.api.tasks.dispatch(task)

    async def _broadcast_scheduled_message(
        self,
        task,
        content: str,
        *,
        stored_message=None,
        attachments=None,
    ) -> None:
        await self.api.delivery.broadcast_scheduled_message(
            task,
            content,
            stored_message=stored_message,
            attachments=attachments,
        )

    @staticmethod
    def _input_image_sources(input_data: ChatInput, *, attachments=None):
        return input_image_sources(input_data, attachments=attachments)

    @staticmethod
    def _input_attachments(input_data: ChatInput):
        return input_attachments(input_data)

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

    def _get_skills_storage(self) -> SkillsStorageLocal:
        skills_storage = getattr(self, "skills_storage", None)
        if isinstance(skills_storage, SkillsStorageLocal):
            return skills_storage
        storage = SkillsStorageLocal(self._get_skills_root())
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
        return [memory_dir / scope for scope in ("daily", "weekly", "monthly", "yearly", "relationships")]

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
        return app

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        heartbeat = create_runtime_heartbeat(
            self.agent,
            self.config.get("runtime") if isinstance(self.config, dict) else None,
            logger_=self.logger,
            subconscious_delivery_sink=self.api.deliver_subconscious_message,
            subconscious_deliverable_channels={"api"},
        )
        try:
            if heartbeat is not None:
                await heartbeat.start()
                self.logger.info(
                    "Runtime heartbeat started (interval=%ss)",
                    heartbeat.interval_seconds,
                )
            await self.api.start()
            yield
        finally:
            await self.api.stop()
            if heartbeat is not None:
                await heartbeat.stop()
                self.logger.info("Runtime heartbeat stopped")

    def _add_routes(self, app: FastAPI) -> None:
        self.api.register_routes(app)
        register_admin_routes(
            app,
            self,
            workspace_text_limit=_WORKSPACE_TEXT_READ_LIMIT,
            workspace_search_text_limit=_WORKSPACE_SEARCH_TEXT_LIMIT,
        )

    def run(self, host: str = None, port: int = None) -> None:
        host = host if host is not None else BaseAgentConfig.DEFAULT_HOST
        port = port if port is not None else BaseAgentConfig.DEFAULT_PORT

        self.logger.info("Starting xAgent API Server on %s:%s", host, port)
        self.logger.info("Model: %s", self.agent.model)
        self.logger.info("Tools: %d loaded", len(self.agent.tools))

        uvicorn.run(self.app, host=host, port=port)
