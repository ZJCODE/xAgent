"""Localhost WebSocket server for one world process."""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import HELLO_TIMEOUT_SECONDS, MAX_MESSAGE_BYTES
from .clock import Clock
from .http import process_http_request
from .models import ClientMessage, encode_error
from .scene import SceneConfig, load_scene_file
from .store import open_store_for_scene
from .world import World

logger = logging.getLogger(__name__)


class WorldServer:
    def __init__(self, world: World, *, host: str, port: int):
        self.world = world
        self.host = host
        self.port = port
        self._server: Optional[Server] = None
        self._world_closed = False

    @classmethod
    def from_scene_path(
        cls,
        scene_path: str,
        *,
        host: str,
        port: int,
        data_root: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> "WorldServer":
        scene = load_scene_file(scene_path)
        return cls.from_scene(scene, host=host, port=port, data_root=data_root, clock=clock)

    @classmethod
    def from_scene(
        cls,
        scene: SceneConfig,
        *,
        host: str,
        port: int,
        data_root: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> "WorldServer":
        from pathlib import Path

        root = Path(data_root).expanduser() if data_root else None
        store = open_store_for_scene(scene, root=root, clock=clock)
        world = World(store, scene, clock=clock)
        return cls(world, host=host, port=port)

    async def start(self) -> int:
        if self._server is not None:
            return self.port
        await self.world.start()
        self._server = await serve(
            self._handler,
            self.host,
            self.port,
            process_request=self._process_request,
            max_size=MAX_MESSAGE_BYTES,
        )
        socks = self._server.sockets
        if not socks:
            raise RuntimeError("world server failed to bind")
        self.port = int(socks[0].getsockname()[1])
        logger.info(
            "agents-world world=%s listening on http://%s:%s (websocket on the same port)",
            self.world.world_id,
            self.host,
            self.port,
        )
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if not self._world_closed:
            await self.world.close()
            self._world_closed = True

    async def run(self) -> None:
        await self.start()
        try:
            if self._server is None:
                return
            await self._server.serve_forever()
        finally:
            await self.stop()

    def _process_request(self, connection: ServerConnection, request):
        # Page sidecar only. World protocol is the WebSocket upgrade path.
        return process_http_request(connection, request, store=self.world.store)

    async def _handler(self, websocket: ServerConnection) -> None:
        session = None
        try:
            try:
                raw = await asyncio.wait_for(websocket.recv(), timeout=HELLO_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                await websocket.send(
                    encode_error("hello_timeout", "first message must be hello")
                )
                await websocket.close(1000, "hello timeout")
                return
            if not isinstance(raw, str):
                raw = raw.decode("utf-8")
            try:
                hello = ClientMessage.from_json(raw)
            except Exception as exc:
                await websocket.send(encode_error("bad_payload", str(exc)))
                return
            if hello.type != "hello":
                await websocket.send(
                    encode_error("bad_payload", "first message must be hello")
                )
                return
            member_id = str(hello.payload.get("member_id") or "").strip()
            display_name = str(hello.payload.get("display_name") or member_id).strip()
            try:
                session = await self.world.attach(
                    member_id=member_id,
                    display_name=display_name,
                    send=websocket.send,
                )
            except ValueError as exc:
                await websocket.send(encode_error("bad_payload", str(exc)))
                return
            await self.world.welcome(session)

            recv_task = asyncio.create_task(self._recv_loop(websocket, session))
            replaced_task = asyncio.create_task(session.replaced.wait())
            done, pending = await asyncio.wait(
                {recv_task, replaced_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            if replaced_task in done:
                try:
                    await websocket.close(1000, "replaced")
                except Exception:
                    pass
            if recv_task in done and not recv_task.cancelled():
                exc = recv_task.exception()
                if exc is not None:
                    raise exc
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.exception("connection error: %s", exc)
            try:
                await websocket.send(encode_error("internal", str(exc)))
            except Exception:
                pass
        finally:
            if session is not None:
                session.replaced.set()
                await self.world.detach(session)

    async def _recv_loop(self, websocket: ServerConnection, session) -> None:
        try:
            async for message in websocket:
                if not isinstance(message, str):
                    message = message.decode("utf-8")
                try:
                    parsed = ClientMessage.from_json(message)
                except Exception as exc:
                    self.world.push_error(session, "bad_payload", str(exc))
                    continue
                if parsed.type == "hello":
                    self.world.push_error(session, "bad_payload", "already greeted")
                    continue
                await self.world.handle(session, parsed.type, parsed.payload)
        except ConnectionClosed:
            return
