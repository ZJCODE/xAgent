"""Push delivery for api channel subscribers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import WebSocket

from ...core.agent import Agent
from ...core.runtime import SubconsciousDelivery
from ...interfaces.server.serializers import message_item
from ...schemas.attachment import dedupe_attachments
from .constants import CHANNEL_API


class DeliveryBus:
    """Broadcast scheduled and subconscious messages to WebSocket subscribers."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def register_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            self._subscribers.setdefault(user_id, set()).add(websocket)

    async def unregister_subscriber(self, user_id: str, websocket: WebSocket) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(user_id)
            if subscribers is None:
                return
            subscribers.discard(websocket)
            if not subscribers:
                self._subscribers.pop(user_id, None)

    async def push(self, user_id: str, payload: Dict[str, Any]) -> None:
        async with self._lock:
            subscribers = list(self._subscribers.get(user_id, set()))
        stale: list[WebSocket] = []
        for websocket in subscribers:
            try:
                await websocket.send_json(payload)
            except Exception:
                stale.append(websocket)
        if stale:
            async with self._lock:
                registered = self._subscribers.get(user_id)
                if registered is not None:
                    for websocket in stale:
                        registered.discard(websocket)
                    if not registered:
                        self._subscribers.pop(user_id, None)

    async def broadcast_scheduled_message(
        self,
        task,
        content: str,
        *,
        stored_message=None,
        attachments: Optional[list[Dict[str, Any]]] = None,
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
        await self.push(user_id, payload)

    async def broadcast_job_message(self, job, content: str) -> None:
        user_id = str(job.target.get("user_id") or job.delivery_user_id or "")
        if not user_id:
            return
        payload: Dict[str, Any] = {
            "type": "job_message",
            "content": content,
            "job": job.to_job_view(),
        }
        await self.push(user_id, payload)

    async def deliver_subconscious(self, delivery: SubconsciousDelivery, *, agent: Agent) -> None:
        if delivery.recipient.channel != CHANNEL_API:
            raise ValueError(f"api runtime cannot deliver subconscious channel {delivery.recipient.channel!r}")
        target = delivery.recipient.target
        user_id = str(target.get("user_id") or delivery.recipient.user_id or "").strip()
        if not user_id:
            raise ValueError("subconscious delivery is missing user_id")

        message_handler = getattr(agent, "message_handler", None)
        store_model_reply = getattr(message_handler, "store_model_reply", None)
        stored_message = None
        if callable(store_model_reply):
            stored_message = await store_model_reply(
                delivery.content,
                getattr(agent, "_assistant_sender_id", "agent"),
                metadata={
                    "subconscious": {
                        "source": "subconscious",
                        "created_at": delivery.created_at.isoformat(sep=" "),
                        "recipient": {
                            "channel": delivery.recipient.channel,
                            "user_id": delivery.recipient.user_id,
                            "target": delivery.recipient.target,
                        },
                    }
                },
                channel=delivery.recipient.channel,
                recipient_id=user_id,
            )

        payload: Dict[str, Any] = {
            "type": "subconscious_message",
            "content": delivery.content,
            "subconscious": {
                "created_at": delivery.created_at.isoformat(sep=" "),
            },
        }
        if stored_message is not None:
            payload["message"] = message_item(stored_message)
        await self.push(user_id, payload)
