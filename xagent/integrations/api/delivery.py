"""Push delivery for api channel subscribers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import WebSocket

from ...core.delivery import DeliveryAck, DeliveryContext
from ...interfaces.server.serializers import message_item
from ...schemas.attachment import dedupe_attachments
from .constants import CHANNEL_API


class DeliveryBus:
    """Broadcast scheduled and initiative messages to WebSocket subscribers."""

    def __init__(self, *, logger: Optional[logging.Logger] = None) -> None:
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self._subscribers: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    def has_subscribers(self, user_id: str) -> bool:
        return bool(self._subscribers.get(str(user_id or "").strip()))

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


class ApiDeliverySession:
    """Unified outbound session for HTTP/WebSocket subscribers."""

    def __init__(self, bus: DeliveryBus, context: DeliveryContext) -> None:
        self.bus = bus
        self.context = context

    def _user_id(self) -> str:
        return str(self.context.user_id or self.context.target.get("user_id") or "").strip()

    def _source(self) -> str:
        return str((self.context.metadata or {}).get("source") or "").strip()

    def can_deliver(self) -> bool:
        source = self._source()
        if source in {"initiative", "scheduled_task"}:
            return self.bus.has_subscribers(self._user_id())
        return True

    async def consume(self, event: dict) -> DeliveryAck:
        if event.get("type") != "message_done":
            return DeliveryAck(accepted=True)
        source = self._source()
        if source not in {"initiative", "scheduled_task"}:
            return DeliveryAck(accepted=True)
        user_id = self._user_id()
        if not user_id:
            return DeliveryAck(accepted=False, error="missing user_id")
        content = str(event.get("content") or "")
        raw_attachments = event.get("attachments")
        attachments = dedupe_attachments(raw_attachments if isinstance(raw_attachments, list) else [])
        if source == "scheduled_task":
            task = (self.context.metadata or {}).get("task")
            if task is None:
                await self.bus.push(
                    user_id,
                    {
                        "type": "scheduled_message",
                        "content": content,
                        **({"attachments": attachments} if attachments else {}),
                    },
                )
            else:
                await self.bus.broadcast_scheduled_message(
                    task,
                    content,
                    attachments=attachments,
                )
            return DeliveryAck(accepted=True)
        if not self.bus.has_subscribers(user_id):
            return DeliveryAck(accepted=False, error="no subscribers")
        payload: Dict[str, Any] = {
            "type": "assistant_message",
            "content": content,
        }
        if attachments:
            payload["attachments"] = attachments
        await self.bus.push(user_id, payload)
        return DeliveryAck(accepted=True)

    async def aclose(self) -> None:
        return None
