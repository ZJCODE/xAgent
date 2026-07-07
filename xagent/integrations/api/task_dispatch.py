"""Scheduled task dispatch for the api channel."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from ...interfaces.server.serializers import response_payload
from ...schemas.attachment import dedupe_attachments
from .chat_service import ChatService
from .constants import CHANNEL_API
from .delivery import DeliveryBus


@dataclass(frozen=True)
class ScheduledTaskResult:
    content: str
    attachments: List[Dict[str, Any]] = field(default_factory=list)


class TaskDispatchService:
    """Execute and deliver scheduled tasks for api channel recipients."""

    def __init__(
        self,
        agent: Agent,
        *,
        chat: ChatService,
        delivery: DeliveryBus,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.chat = chat
        self.delivery = delivery
        self.logger = logger or logging.getLogger(self.__class__.__name__)

    def can_handle(self, task) -> bool:
        if task.kind != "task":
            return False
        return task.delivery_channel == CHANNEL_API

    async def dispatch(self, task) -> None:
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
            message_handler = getattr(self.agent, "message_handler", None)
            store_model_reply = getattr(message_handler, "store_model_reply", None)
            if callable(store_model_reply):
                stored_message = await store_model_reply(
                    result.content,
                    getattr(self.agent, "_assistant_sender_id", "agent"),
                    metadata=metadata,
                    attachments=result.attachments,
                    channel=CHANNEL_API,
                    recipient_id=task.delivery_user_id or str(task.target.get("user_id") or ""),
                )
        await self.delivery.broadcast_scheduled_message(
            task,
            result.content,
            stored_message=stored_message,
            attachments=result.attachments,
        )

    async def _scheduled_task_result(self, task) -> ScheduledTaskResult:
        task_type = task.task_type
        if task_type == "message":
            return ScheduledTaskResult(task.content.strip())
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
        await self.chat.acquire_slot()
        try:
            deadline = time.monotonic() + self.chat._chat_timeout
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
                response = await self.chat._await_before_deadline(
                    chat(
                        user_message=prompt,
                        user_id=user_id,
                    ),
                    deadline,
                )
        finally:
            self.chat.release_slot()
        return self._scheduled_response_result(response)

    async def _scheduled_agent_event_result(
        self,
        chat_events,
        *,
        prompt: str,
        user_id: str,
        deadline: float,
    ) -> ScheduledTaskResult:
        final_content = ""
        final_attachments: List[Dict[str, Any]] = []
        last_error = ""
        async for event in self.chat._iterate_before_deadline(
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
            return ScheduledTaskResult(final_content, final_attachments)
        return ScheduledTaskResult(last_error)

    @staticmethod
    def _scheduled_response_result(response: Any) -> ScheduledTaskResult:
        result = response_payload(response)
        if isinstance(result, str):
            return ScheduledTaskResult(result.strip())
        return ScheduledTaskResult(json.dumps(result, ensure_ascii=False).strip())
