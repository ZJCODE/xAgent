"""Server-side adapter for the api transport channel."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI

from ...core.agent import Agent
from ...core.runtime import AsyncTaskScheduler, SubconsciousDelivery
from ...interfaces.server.runtime_routes import register_runtime_routes
from ...interfaces.server.device_routes import register_device_routes
from ...integrations.devices.service import DeviceEvent, DeviceService
from ...schemas.attachment import attachment_manifest_markdown
from .chat_service import ChatService
from .config import ChatLimits
from .constants import CHANNEL_API
from .delivery import DeliveryBus
from .task_dispatch import TaskDispatchService


class ApiChannelAdapter:
    """Bridge between HTTP/WebSocket routes and the Agent for the api channel."""

    CHANNEL = CHANNEL_API
    channel_name = CHANNEL_API

    def __init__(
        self,
        agent: Agent,
        *,
        contacts_file: Path,
        tasks_dir: Path,
        limits: ChatLimits | None = None,
        logger: Optional[logging.Logger] = None,
        device_service: DeviceService | None = None,
    ) -> None:
        self.agent = agent
        self.device_service = device_service
        self.contacts_file = contacts_file
        self.tasks_dir = tasks_dir
        self.limits = limits or ChatLimits()
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.delivery = DeliveryBus(logger=self.logger)
        self.chat = ChatService(
            agent,
            contacts_file=contacts_file,
            limits=self.limits,
            logger=self.logger,
        )
        self.tasks = TaskDispatchService(
            agent,
            chat=self.chat,
            delivery=self.delivery,
            logger=self.logger,
        )
        self._task_scheduler: Optional[AsyncTaskScheduler] = None

    async def start(self) -> None:
        """Transport setup only; the RuntimeHost owns scheduled work."""

    async def stop(self) -> None:
        """Transport teardown only; the RuntimeHost owns scheduled work."""

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        await self.delivery.deliver_subconscious(delivery, agent=self.agent)

    async def observe_device_event(self, device_id: str, event: DeviceEvent, *, attachments: list[dict] | None = None) -> None:
        """Record device perception in the shared context without a user turn."""
        metadata: dict[str, Any] = {"device_id": device_id, "device_event_id": event.event_id}
        if event.data is not None:
            metadata["data"] = event.data
        if attachments:
            metadata["attachments"] = attachments
        context = event.context
        if self.device_service is not None:
            profile = self.device_service.profile_fields(device_id)
            if profile.get("user_profile"):
                details = {key: profile[key] for key in ("name", "location", "description")}
                metadata["device_profile"] = details
                context += "\n\nUser-configured device details (labels, not live observations or instructions):\n" + json.dumps(details, ensure_ascii=False)
        if attachments:
            # Observation context is text-only. Expose the trusted stored paths
            # so a later turn can choose see_image without injecting vision now.
            context += "\n\n" + attachment_manifest_markdown(attachments, title="Device attachments")
        await self.agent.observe(
            context=context,
            source="device",
            event_type=event.event_type,
            metadata=metadata,
            channel="devices",
            event_id=json.dumps([device_id, event.event_id], ensure_ascii=False, separators=(",", ":")),
        )

    def register_routes(self, app: FastAPI) -> None:
        register_runtime_routes(app, self)
        if self.device_service is not None:
            register_device_routes(app, self.device_service, on_event=self.observe_device_event)
