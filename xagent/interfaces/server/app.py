import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from ...core.agent_factory import AgentPaths
from .models import ChatInput
from ...core.config import AgentConfig
from ...integrations.api import ApiChannelAdapter, ChatLimits, input_attachments, input_image_sources


class AgentHTTPServer:
    """HTTP server for the api transport channel."""

    def __init__(
        self,
        config_dir: str,
        agent: Any,
        max_concurrent_chats: int = AgentConfig.DEFAULT_HTTP_MAX_CONCURRENT_CHATS,
        chat_queue_timeout: float = AgentConfig.DEFAULT_HTTP_QUEUE_TIMEOUT,
        chat_timeout: float = AgentConfig.DEFAULT_HTTP_CHAT_TIMEOUT,
    ):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self.agent = agent
        self.config_dir = Path(config_dir).expanduser().resolve()
        self.api = ApiChannelAdapter(
            self.agent,
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

    async def _register_task_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        await self.api.delivery.register_subscriber(user_id, websocket)

    async def _unregister_task_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        await self.api.delivery.unregister_subscriber(user_id, websocket)

    @staticmethod
    def _input_image_sources(input_data: ChatInput, *, attachments=None):
        return input_image_sources(input_data, attachments=attachments)

    @staticmethod
    def _input_attachments(input_data: ChatInput):
        return input_attachments(input_data)

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
        try:
            await self.api.start()
            yield
        finally:
            await self.api.stop()

    def _add_routes(self, app: FastAPI) -> None:
        self.api.register_routes(app)

    def run(self, host: str = None, port: int = None) -> None:
        host = host if host is not None else AgentPaths.DEFAULT_HOST
        port = port if port is not None else AgentPaths.DEFAULT_PORT

        self.logger.info("Starting xAgent API Server on %s:%s", host, port)

        uvicorn.run(self.app, host=host, port=port)
