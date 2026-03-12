"""OpenAI Realtime provider bridge."""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Awaitable, Callable, Optional

from ...runtime import AsyncOpenAI
from ..events import RealtimeServerEvent


EventSink = Optional[Callable[[RealtimeServerEvent], Awaitable[None] | None]]


class OpenAIRealtimeBridge:
    """Thin adapter around the OpenAI realtime client when available."""

    def __init__(
        self,
        model: str = "gpt-realtime",
        voice: str = "alloy",
        client: Optional[Any] = None,
    ):
        self.model = model
        self.voice = voice
        self.client = client
        self._connection_cm: Any = None
        self._connection: Any = None
        self._listener_task: Optional[asyncio.Task] = None
        self._event_sink: EventSink = None

    async def connect(self, event_sink: EventSink = None) -> bool:
        self._event_sink = event_sink
        if self.client is None:
            try:
                self.client = AsyncOpenAI()
            except Exception:
                return False
        if not hasattr(self.client, "realtime"):
            return False

        try:
            self._connection_cm = self.client.realtime.connect(model=self.model)
            self._connection = await self._connection_cm.__aenter__()
            if hasattr(self._connection, "session") and hasattr(self._connection.session, "update"):
                await self._connection.session.update(
                    session={
                        "modalities": ["text", "audio"],
                        "voice": self.voice,
                    }
                )
            self._listener_task = asyncio.create_task(self._listen())
            return True
        except Exception:
            return False

    async def _listen(self) -> None:
        if self._connection is None:
            return

        try:
            async for event in self._connection:
                normalized = self._normalize_event(event)
                if normalized is None or self._event_sink is None:
                    continue
                maybe_awaitable = self._event_sink(normalized)
                if maybe_awaitable is not None:
                    await maybe_awaitable
        except Exception:
            return

    def _normalize_event(self, event: Any) -> Optional[RealtimeServerEvent]:
        event_type = getattr(event, "type", "")
        if event_type == "response.text.delta":
            return RealtimeServerEvent(type="partial_text", payload={"delta": getattr(event, "delta", "")})
        if event_type == "response.audio.delta":
            return RealtimeServerEvent(type="partial_audio", payload={"delta": getattr(event, "delta", "")})
        if event_type == "response.done":
            return RealtimeServerEvent(type="turn.completed", payload={"provider_event": event_type})
        if event_type == "session.updated":
            return RealtimeServerEvent(type="session.state", payload={"provider_event": event_type})
        return None

    async def send_text(self, text: str) -> bool:
        if self._connection is None or not text:
            return False
        try:
            await self._connection.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}],
                }
            )
            await self._connection.response.create()
            return True
        except Exception:
            return False

    async def append_audio_chunk(self, audio_chunk: str) -> bool:
        if self._connection is None or not audio_chunk:
            return False
        try:
            await self._connection.input_audio_buffer.append(audio=audio_chunk)
            return True
        except Exception:
            return False

    async def commit_audio(self) -> bool:
        if self._connection is None:
            return False
        try:
            await self._connection.input_audio_buffer.commit()
            await self._connection.response.create()
            return True
        except Exception:
            return False

    async def interrupt(self) -> bool:
        if self._connection is None:
            return False
        try:
            await self._connection.response.cancel()
            return True
        except Exception:
            return False

    async def close(self) -> None:
        if self._listener_task is not None:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        if self._connection_cm is not None:
            with contextlib.suppress(Exception):
                await self._connection_cm.__aexit__(None, None, None)
