"""Reference client for the agents-world WebSocket protocol."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any, AsyncIterator, Callable, Optional

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from . import MAX_MESSAGE_BYTES

Predicate = Callable[[dict[str, Any]], bool]


class MemberTakenError(RuntimeError):
    """The hub refused hello: this member_id is embodied by another live connection."""

    def __init__(self, member_id: str, message: str = ""):
        super().__init__(message or f"member_id is present from another connection: {member_id}")
        self.member_id = member_id


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
        resume_token: str = "",
    ):
        self.url = url
        self.member_id = member_id
        self.display_name = display_name or member_id
        # Set from `welcome`; pass it back on reconnect to take over the same member_id.
        self.resume_token = str(resume_token or "")
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.welcome: Optional[dict[str, Any]] = None
        self.last_seq: int = 0
        self._ws: Optional[ClientConnection] = None
        self._reader: Optional[asyncio.Task[None]] = None

    async def connect(self) -> dict[str, Any]:
        self._ws = await connect(self.url, max_size=MAX_MESSAGE_BYTES)
        hello: dict[str, Any] = {
            "type": "hello",
            "member_id": self.member_id,
            "display_name": self.display_name,
        }
        if self.resume_token:
            hello["resume_token"] = self.resume_token
        await self._send(hello)
        self._reader = asyncio.create_task(self._read_loop(), name=f"client-{self.member_id}")
        try:
            msg = await self.recv(timeout=10.0)
        except BaseException:
            await self.close()
            raise
        if msg.get("type") != "welcome":
            await self.close()
            if msg.get("type") == "error" and msg.get("code") == "member_taken":
                raise MemberTakenError(self.member_id, str(msg.get("message") or ""))
            raise RuntimeError(f"expected welcome, got {msg}")
        self.welcome = msg
        self.resume_token = str(msg.get("resume_token") or self.resume_token or "")
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

    async def join(self) -> None:
        await self._send({"type": "join"})

    async def leave(self) -> None:
        await self._send({"type": "leave"})

    async def speak(
        self,
        text: str,
        mentions: Optional[list[str]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        body: dict[str, Any] = {"type": "speak", "text": text}
        if mentions:
            body["mentions"] = mentions
        if attachments:
            body["attachments"] = [_encode_attachment(item) for item in attachments]
        await self._send(body)

    async def sync(self, after_seq: int = 0) -> None:
        await self._send({"type": "sync", "after_seq": after_seq})

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
                if msg.get("type") == "event" and "seq" in msg:
                    seq = int(msg["seq"])
                    self.last_seq = max(self.last_seq, seq)
                await self.inbox.put(msg)
        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            pass
        except Exception:
            pass
        finally:
            await self.inbox.put({"type": "_closed"})


def _encode_attachment(item: dict[str, Any]) -> dict[str, Any]:
    encoded: dict[str, Any] = {}
    name = str(item.get("name") or "").strip()
    mime = str(item.get("mime") or "").strip()
    file_id = str(item.get("id") or "").strip()
    if name:
        encoded["name"] = name
    if mime:
        encoded["mime"] = mime
    if file_id:
        encoded["id"] = file_id
    data = item.get("data")
    if isinstance(data, bytes):
        encoded["data"] = base64.b64encode(data).decode("ascii")
    elif isinstance(data, str) and data:
        encoded["data"] = data
    return encoded
