"""Reference client for the agents-env WebSocket protocol."""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator, Callable, Optional

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

Predicate = Callable[[dict[str, Any]], bool]


class WorldClient:
    """Minimal inhabitant: hello, join, speak, leave, sync, and an inbox of perceptions.

    This is the executable protocol spec. Agent adapters should depend on this
    (or the same messages) rather than re-implementing the handshake.
    """

    def __init__(
        self,
        url: str,
        *,
        member_id: str,
        display_name: str = "",
    ):
        self.url = url
        self.member_id = member_id
        self.display_name = display_name or member_id
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.welcome: Optional[dict[str, Any]] = None
        self.last_seq: dict[str, int] = {}
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional[asyncio.Task[None]] = None

    async def connect(self) -> dict[str, Any]:
        self._ws = await connect(self.url)
        await self._send(
            {
                "type": "hello",
                "member_id": self.member_id,
                "display_name": self.display_name,
            }
        )
        self._reader = asyncio.create_task(self._read_loop(), name=f"client-{self.member_id}")
        msg = await self.recv(timeout=10.0)
        if msg.get("type") != "welcome":
            raise RuntimeError(f"expected welcome, got {msg}")
        self.welcome = msg
        return msg

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def __aenter__(self) -> "WorldClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def join(self, room_id: str) -> None:
        await self._send({"type": "join", "room_id": room_id})

    async def leave(self, room_id: str) -> None:
        await self._send({"type": "leave", "room_id": room_id})

    async def speak(
        self,
        room_id: str,
        text: str,
        mentions: Optional[list[str]] = None,
    ) -> None:
        body: dict[str, Any] = {"type": "speak", "room_id": room_id, "text": text}
        if mentions:
            body["mentions"] = mentions
        await self._send(body)

    async def sync(self, room_id: str, after_seq: int = 0) -> None:
        await self._send({"type": "sync", "room_id": room_id, "after_seq": after_seq})

    async def recv(self, timeout: Optional[float] = None) -> dict[str, Any]:
        if timeout is None:
            return await self.inbox.get()
        return await asyncio.wait_for(self.inbox.get(), timeout)

    async def wait_for(self, predicate: Predicate, *, timeout: float = 5.0) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("timed out waiting for matching message")
            msg = await asyncio.wait_for(self.inbox.get(), remaining)
            if predicate(msg):
                return msg

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            msg = await self.inbox.get()
            if msg.get("type") == "_closed":
                return
            yield msg

    async def _send(self, body: dict[str, Any]) -> None:
        if self._ws is None:
            raise RuntimeError("client is not connected")
        await self._ws.send(json.dumps(body, ensure_ascii=False))

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if not isinstance(raw, str):
                    raw = raw.decode("utf-8")
                msg = json.loads(raw)
                if msg.get("type") == "event" and "seq" in msg and msg.get("room_id"):
                    seq = int(msg["seq"])
                    room_id = str(msg["room_id"])
                    self.last_seq[room_id] = max(self.last_seq.get(room_id, 0), seq)
                await self.inbox.put(msg)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            await self.inbox.put({"type": "_closed"})
