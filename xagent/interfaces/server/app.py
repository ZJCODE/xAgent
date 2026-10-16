import logging
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..base import BaseAgentConfig
from .admin_service import AdminService
from .admin_routes import register_admin_routes
from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import (
    SubconsciousDelivery,
    create_runtime_heartbeat,
    resolve_contacts_path,
)
from ...integrations.api import ApiChannelAdapter, ChatLimits


class AgentHTTPServer(AdminService):
    """HTTP server for the api transport channel."""

    def __init__(
        self,
        config_dir: Optional[str] = None,
        agent: Optional[Agent] = None,
        max_concurrent_chats: int = AgentConfig.DEFAULT_HTTP_MAX_CONCURRENT_CHATS,
        chat_queue_timeout: float = AgentConfig.DEFAULT_HTTP_QUEUE_TIMEOUT,
        chat_timeout: float = AgentConfig.DEFAULT_HTTP_CHAT_TIMEOUT,
        runtime_managed: bool = False,
        api_adapter: Optional[ApiChannelAdapter] = None,
    ):
        super().__init__(config_dir=config_dir, agent=agent,
                         device_service=getattr(api_adapter, "device_service", None))
        self.runtime_managed = runtime_managed
        self.is_runtime_owner = runtime_managed

        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        contacts_file = resolve_contacts_path(self.workspace)
        channels_cfg = self.config.get("channels") if isinstance(self.config, dict) else None
        raw_voice_cfg = channels_cfg.get("voice") if isinstance(channels_cfg, dict) else None
        if api_adapter is not None and not api_adapter.voice_config and isinstance(raw_voice_cfg, dict):
            api_adapter.voice_config = dict(raw_voice_cfg)
        self.api = api_adapter or ApiChannelAdapter(
            self.agent,
            contacts_file=contacts_file,
            tasks_dir=self.tasks_dir,
            limits=ChatLimits(
                max_concurrent_chats=max_concurrent_chats,
                chat_queue_timeout=chat_queue_timeout,
                chat_timeout=chat_timeout,
            ),
            logger=self.logger,
            device_service=self.device_service,
            voice_config=raw_voice_cfg if isinstance(raw_voice_cfg, dict) else None,
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
        """HTTP transport never owns agent background work or maintenance."""
        yield

    def _add_routes(self, app: FastAPI) -> None:
        self.api.register_routes(app)
        register_admin_routes(app, lambda: self)

    def run(self, host: str = None, port: int = None) -> None:
        if not self.runtime_managed:
            raise RuntimeError("Use xagent run/start; RuntimeHost owns the API lifecycle.")
        host = host if host is not None else BaseAgentConfig.DEFAULT_HOST
        port = port if port is not None else BaseAgentConfig.DEFAULT_PORT

        self.logger.info("Starting xAgent API Server on %s:%s", host, port)
        self.logger.info("Model: %s", self.agent.model)
        self.logger.info("Tools: %d loaded", len(self.agent.tools))

        uvicorn.run(self.app, host=host, port=port)
