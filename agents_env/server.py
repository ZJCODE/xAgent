"""Localhost WebSocket server for one world process."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from .models import ClientMessage
from .scene import SceneConfig, load_scene_file
from .store import open_store_for_scene
from .world import World

logger = logging.getLogger(__name__)


class WorldServer:
    def __init__(self, world: World, *, host: str, port: int):
        self.world = world
        self.host = host
        self.port = port
        self._server = None

    @classmethod
    def from_scene_path(
        cls,
        scene_path: str,
        *,
        host: str,
        port: int,
        data_root: Optional[str] = None,
    ) -> "WorldServer":
        scene = load_scene_file(scene_path)
        return cls.from_scene(scene, host=host, port=port, data_root=data_root)

    @classmethod
    def from_scene(
        cls,
        scene: SceneConfig,
        *,
        host: str,
        port: int,
        data_root: Optional[str] = None,
    ) -> "WorldServer":
        from pathlib import Path

        root = Path(data_root).expanduser() if data_root else None
        store = open_store_for_scene(scene, root=root)
        world = World(store, scene)
        return cls(world, host=host, port=port)

    async def run(self) -> None:
        await self.world.start()
        async with serve(self._handler, self.host, self.port) as server:
            self._server = server
            logger.info(
                "agents-env world=%s listening on ws://%s:%s",
                self.world.world_id,
                self.host,
                self.port,
            )
            await server.serve_forever()

    async def _handler(self, websocket: ServerConnection) -> None:
        session = None
        send_lock = asyncio.Lock()

        async def send(raw: str) -> None:
            async with send_lock:
                await websocket.send(raw)

        try:
            raw = await websocket.recv()
            if not isinstance(raw, str):
                raw = raw.decode("utf-8")
            hello = ClientMessage.from_json(raw)
            if hello.type != "hello":
                await send('{"type":"error","message":"first message must be hello"}')
                return
            member_id = str(hello.payload.get("member_id") or "").strip()
            display_name = str(hello.payload.get("display_name") or member_id).strip()
            session = await self.world.attach(
                member_id=member_id,
                display_name=display_name,
                send=send,
            )
            await self.world.welcome(session)

            async for message in websocket:
                if not isinstance(message, str):
                    message = message.decode("utf-8")
                try:
                    parsed = ClientMessage.from_json(message)
                except Exception as exc:
                    await send(f'{{"type":"error","message":{_json_str(str(exc))}}}')
                    continue
                if parsed.type == "hello":
                    await send('{"type":"error","message":"already greeted"}')
                    continue
                await self.world.handle(session, parsed.type, parsed.payload)
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.exception("connection error: %s", exc)
            try:
                await send(f'{{"type":"error","message":{_json_str(str(exc))}}}')
            except Exception:
                pass
        finally:
            if session is not None:
                await self.world.detach(session)


def _json_str(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)
