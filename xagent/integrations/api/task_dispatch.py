"""Scheduled task dispatch for the api channel."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import ScheduledDeliveryContext, current_delivery_context, scheduled_delivery_context
from ...interfaces.server.serializers import response_payload
from ...schemas.attachment import dedupe_attachments
from ...schemas.attachment import ATTACHMENT_METADATA_KEY
from ...schemas import Message, RoleType
from ...core.runtime.task_receipts import DeliveryUncertainError, occurrence_run_id
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
        # ``local`` records predate channel-scoped delivery; the api channel is
        # the default home surface, so it adopts them instead of leaving them undeliverable.
        return task.delivery_channel in (CHANNEL_API, "", "local")

    async def dispatch(self, task) -> None:
        result = await self.execute(task)
        await self.deliver(task, result, occurrence_run_id(task.task_id, task.run_at))

    async def execute(self, task) -> dict[str, Any]:
        result = await self._scheduled_task_result(task)
        if not result.content and not result.attachments:
            raise ValueError("scheduled task produced no content")
        return {"content": result.content, "attachments": result.attachments}

    async def deliver(self, task, result: dict[str, Any], run_id: str) -> dict[str, Any]:
        user_id = task.delivery_user_id or str(task.target.get("user_id") or "")
        if not user_id:
            raise ValueError("scheduled API task is missing user_id")
        metadata = {
            "scheduled_run_id": run_id,
            "scheduled_task": {
                "id": task.task_id,
                "name": task.name,
                "type": task.task_type,
                "run_at": task.run_at.isoformat(sep=" "),
                "delivery": task.delivery,
                "run_id": run_id,
            }
        }
        stored_message = None
        storage = getattr(self.agent, "message_storage", None)
        get_existing = getattr(storage, "get_message_by_metadata", None)
        add_once = getattr(storage, "add_message_once", None)
        if callable(get_existing) and callable(add_once):
            stored_message = await get_existing("scheduled_run_id", run_id, role=RoleType.ASSISTANT)
            if stored_message is None:
                message = Message.create(content=str(result.get("content") or ""), role=RoleType.ASSISTANT,
                                         sender_id=getattr(self.agent, "_assistant_sender_id", "agent"))
                message.channel = CHANNEL_API
                message.recipient_id = user_id
                message.metadata.update(metadata)
                if result.get("attachments"):
                    message.metadata[ATTACHMENT_METADATA_KEY] = dedupe_attachments(result["attachments"])
                stored_message = await add_once(message, idempotency_key=run_id)
        elif task.task_type == "message":
            message_handler = getattr(self.agent, "message_handler", None)
            store_model_reply = getattr(message_handler, "store_model_reply", None)
            if callable(store_model_reply):
                stored_message = await store_model_reply(
                    str(result.get("content") or ""),
                    getattr(self.agent, "_assistant_sender_id", "agent"),
                    metadata=metadata,
                    attachments=result.get("attachments") or [],
                    channel=CHANNEL_API,
                    recipient_id=user_id,
                )
        if stored_message is None:
            raise ValueError("scheduled API delivery requires durable message storage")
        await self.delivery.broadcast_scheduled_message(
            task,
            str(result.get("content") or ""),
            stored_message=stored_message,
            attachments=result.get("attachments") or [],
        )
        return {"accepted": True, "transport": "durable_history",
                "message_cursor": (stored_message.metadata or {}).get(AgentConfig.MESSAGE_STORAGE_CURSOR_KEY)
                if stored_message is not None else None}

    async def _scheduled_task_result(self, task) -> ScheduledTaskResult:
        task_type = task.task_type
        if task_type == "message":
            return ScheduledTaskResult(task.content.strip())
        if task_type != "agent":
            raise ValueError(f"unsupported scheduled task type: {task_type}")

        user_id = task.delivery_user_id or str(task.target.get("user_id") or AgentConfig.DEFAULT_USER_ID)
        prompt = str(task.content or "").strip()
        context = ScheduledDeliveryContext(
            channel=task.delivery_channel,
            user_id=user_id,
            target=task.delivery.get("target") if isinstance(task.delivery.get("target"), dict) else {},
            metadata={
                **(current_delivery_context().metadata if current_delivery_context() else {}),
                "source": "scheduled_task",
                "task_id": task.task_id,
                "task_name": task.name,
                "task_type": task.task_type,
            },
        )
        await self.chat.acquire_slot()
        try:
            chat_events = getattr(self.agent, "chat_events", None)
            if not callable(chat_events):
                raise RuntimeError("Agent does not support chat_events().")
            deadline = time.monotonic() + self.chat._chat_timeout
            with scheduled_delivery_context(context):
                return await self._scheduled_agent_event_result(
                    chat_events,
                    prompt=prompt,
                    user_id=user_id,
                    channel=CHANNEL_API if task.delivery_channel in (CHANNEL_API, "", "local") else task.delivery_channel,
                    deadline=deadline,
                )
        finally:
            self.chat.release_slot()

    async def _scheduled_agent_event_result(
        self,
        chat_events,
        *,
        prompt: str,
        user_id: str,
        channel: str,
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
                channel=channel,
                inbox_kind="scheduled_turn",
            ),
            deadline,
        ):
            event_type = event.get("type")
            if event_type in {"error", "aborted"} and event.get("needs_review"):
                raise DeliveryUncertainError(str(event.get("error") or "scheduled tool outcome is unknown"))
            if event_type == "message_done" and str(event.get("phase") or "final") == "final":
                final_content = str(event.get("content") or "").strip()
                raw_attachments = event.get("attachments")
                final_attachments = dedupe_attachments(raw_attachments if isinstance(raw_attachments, list) else [])
            elif event_type == "error":
                last_error = str(event.get("error") or "").strip()
        if final_content or final_attachments:
            return ScheduledTaskResult(final_content, final_attachments)
        if last_error:
            raise RuntimeError(last_error)
        raise ValueError("scheduled task produced no final result")

    @staticmethod
    def _scheduled_response_result(response: Any) -> ScheduledTaskResult:
        result = response_payload(response)
        if isinstance(result, str):
            return ScheduledTaskResult(result.strip())
        return ScheduledTaskResult(json.dumps(result, ensure_ascii=False).strip())
