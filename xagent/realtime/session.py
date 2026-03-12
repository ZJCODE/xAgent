"""Realtime session manager."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from ..state import ConversationStateStore, LiveSessionState
from .events import RealtimeServerEvent


class RealtimeSessionManager:
    """Manages live realtime sessions and per-session event queues."""

    def __init__(self, state_store: ConversationStateStore):
        self.state_store = state_store
        self._queues: Dict[str, asyncio.Queue[RealtimeServerEvent]] = {}
        self._providers: Dict[str, Any] = {}

    def open(
        self,
        user_id: str,
        conversation_id: Optional[str] = None,
        realtime_session_id: Optional[str] = None,
        provider_name: Optional[str] = None,
    ) -> LiveSessionState:
        session = self.state_store.open_live_session(
            user_id=user_id,
            conversation_id=conversation_id,
            realtime_session_id=realtime_session_id,
            provider_name=provider_name,
        )
        self._queues[session.realtime_session_id] = asyncio.Queue()
        return session

    def get_session(self, realtime_session_id: str) -> Optional[LiveSessionState]:
        return self.state_store.get_live_session(realtime_session_id)

    def get_event_queue(self, realtime_session_id: str) -> asyncio.Queue[RealtimeServerEvent]:
        queue = self._queues.get(realtime_session_id)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[realtime_session_id] = queue
        return queue

    async def emit(self, realtime_session_id: str, event: RealtimeServerEvent) -> None:
        await self.get_event_queue(realtime_session_id).put(event)

    def attach_provider(self, realtime_session_id: str, provider: Any) -> None:
        self._providers[realtime_session_id] = provider

    def get_provider(self, realtime_session_id: str) -> Any:
        return self._providers.get(realtime_session_id)

    async def close(self, realtime_session_id: str) -> Optional[LiveSessionState]:
        provider = self._providers.pop(realtime_session_id, None)
        if provider is not None and hasattr(provider, "close"):
            await provider.close()
        self._queues.pop(realtime_session_id, None)
        return self.state_store.close_live_session(realtime_session_id)
