"""Server-side adapter for the api transport channel."""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI

from ...interfaces.server.runtime_routes import register_runtime_routes
from .chat_service import ChatService
from .config import ChatLimits
from .constants import CHANNEL_API
from .delivery import DeliveryBus


class ApiChannelAdapter:
    """Bridge between HTTP/WebSocket routes and the Agent for the api channel."""

    CHANNEL = CHANNEL_API

    def __init__(
        self,
        agent: Any,
        *,
        limits: ChatLimits | None = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.limits = limits or ChatLimits()
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.delivery = DeliveryBus(logger=self.logger)
        self.chat = ChatService(
            agent,
            limits=self.limits,
            logger=self.logger,
        )

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def register_routes(self, app: FastAPI) -> None:
        register_runtime_routes(app, self)
