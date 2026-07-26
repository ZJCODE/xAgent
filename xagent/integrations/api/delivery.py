"""Push delivery for api channel subscribers."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import WebSocket

class DeliveryBus:
    """Push Runtime deliveries to connected API subscribers."""

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
