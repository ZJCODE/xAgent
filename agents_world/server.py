"""Localhost hub: many worlds on one port; HTTP catalog + /ws/{world_id}."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urlparse

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from . import HELLO_TIMEOUT_SECONDS, MAX_MESSAGE_BYTES
from .clock import Clock, default_clock
from .config import WorldConfig
from .http import process_http_request
from .models import ClientMessage, encode_error
from .paths import (
    allocate_world_id,
    list_world_ids,
    remove_world_dir,
    resolve_data_root,
    validate_world_id,
    world_data_dir,
)
from .store import WorldStore, open_store_for_world
from .world import MemberTaken, World

logger = logging.getLogger(__name__)


class WorldHub:
    """One OS process hosting many World instances."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        data_root: Optional[Path | str] = None,
        clock: Optional[Clock] = None,
    ):
        self.host = host
        self.port = port
        self.data_root = resolve_data_root(data_root)
        self.clock = default_clock(clock)
        self._worlds: dict[str, World] = {}
        self._server: Optional[Server] = None
        self._closed = False

    @classmethod
    def create(
        cls,
        *,
        host: str,
        port: int,
        data_root: Optional[str] = None,
        clock: Optional[Clock] = None,
    ) -> "WorldHub":
        return cls(host=host, port=port, data_root=data_root, clock=clock)

    def get(self, world_id: str) -> Optional[World]:
        return self._worlds.get(world_id)

    def list_summaries(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for world_id in sorted(self._worlds):
            world = self._worlds[world_id]
            present = world.list_live_present()
            out.append(
                {
                    "id": world.world_id,
                    "name": world.store.name,
                    "latest_seq": world.store.max_seq(),
                    "present_count": len(present),
                    "present_by_kind": world.present_by_kind(present),
                }
            )
        return out

    def create_world(self, *, name: str, world_id: str = "") -> World:
        label = str(name or "").strip()
        if not label:
            raise ValueError("name is required")
        if world_id:
            wid = validate_world_id(world_id)
            if wid in self._worlds or (world_data_dir(wid, root=self.data_root) / "world.sqlite3").is_file():
                raise FileExistsError(f"world already exists: {wid}")
        else:
            taken = set(self._worlds) | set(list_world_ids(root=self.data_root))
            wid = allocate_world_id(label, taken=taken)
        config = WorldConfig.create(world_id=wid, name=label)
        store = open_store_for_world(config, root=self.data_root, clock=self.clock)
        world = World(store, config, clock=self.clock)
        self._worlds[wid] = world
        return world

    async def delete_world(self, world_id: str) -> dict[str, Any]:
        """Drop a world from the hub and delete its directory.

        Connected inhabitants are disconnected. The id is then free for create.
        """
        wid = validate_world_id(world_id)
        world = self._worlds.pop(wid, None)
        if world is not None:
            world.disconnect_all(code="world_gone", message=f"world deleted: {wid}")
            await world.close()
        data_dir = world_data_dir(wid, root=self.data_root)
        if world is None and not data_dir.exists():
            raise FileNotFoundError(f"unknown world: {wid}")
        if data_dir.exists():
            remove_world_dir(wid, root=self.data_root)
        return {"id": wid, "deleted": True}

    def _load_existing(self) -> None:
        for world_id in list_world_ids(root=self.data_root):
            if world_id in self._worlds:
                continue
            db = world_data_dir(world_id, root=self.data_root) / "world.sqlite3"
            store = WorldStore(db, world_id=world_id, clock=self.clock)
            config = WorldConfig(world_id=world_id, name=store.name)
            self._worlds[world_id] = World(store, config, clock=self.clock)

    async def start(self) -> int:
        if self._server is not None:
            return self.port
        self.data_root.mkdir(parents=True, exist_ok=True)
        (self.data_root / "worlds").mkdir(parents=True, exist_ok=True)
        self._load_existing()
        for world in self._worlds.values():
            await world.start()
        self._server = await serve(
            self._handler,
            self.host,
            self.port,
            process_request=self._process_request,
            max_size=MAX_MESSAGE_BYTES,
        )
        socks = self._server.sockets
        if not socks:
            raise RuntimeError("world hub failed to bind")
        self.port = int(socks[0].getsockname()[1])
        logger.info(
            "agents-world hub listening on http://%s:%s (%d worlds)",
            self.host,
            self.port,
            len(self._worlds),
        )
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._closed:
            return
        self._closed = True
        for world in list(self._worlds.values()):
            await world.close()
        self._worlds.clear()

    async def run(self) -> None:
        await self.start()
        try:
            if self._server is None:
                return
            await self._server.serve_forever()
        finally:
            await self.stop()

    async def _process_request(self, connection: ServerConnection, request):
        return await process_http_request(connection, request, hub=self)

    def _world_id_from_path(self, path: str) -> Optional[str]:
        parts = [p for p in (path or "").split("/") if p]
        if len(parts) != 2 or parts[0] != "ws":
            return None
        try:
            return validate_world_id(unquote(parts[1]))
        except ValueError:
            return None

    async def _handler(self, websocket: ServerConnection) -> None:
        path = ""
        request = getattr(websocket, "request", None)
        if request is not None:
            path = urlparse(getattr(request, "path", "") or "").path or ""
        world_id = self._world_id_from_path(path)
        if world_id is None:
            try:
                await websocket.send(encode_error("bad_payload", "connect to /ws/{world_id}"))
            except Exception:
                pass
            await websocket.close(1008, "world required")
            return
        world = self._worlds.get(world_id)
        if world is None:
            try:
                await websocket.send(encode_error("unknown_world", f"unknown world: {world_id}"))
            except Exception:
                pass
            await websocket.close(1008, "unknown world")
            return
        await self._serve_world(websocket, world)

    async def _serve_world(self, websocket: ServerConnection, world: World) -> None:
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
            resume_token = str(hello.payload.get("resume_token") or "").strip()
            member_kind = str(hello.payload.get("kind") or "").strip()
            try:
                session = await world.attach(
                    member_id=member_id,
                    display_name=display_name,
                    send=websocket.send,
                    resume_token=resume_token,
                    kind=member_kind,
                )
            except MemberTaken as exc:
                await websocket.send(
                    encode_error(
                        "member_taken",
                        str(exc),
                        member_id=exc.member_id,
                    )
                )
                await websocket.close(1008, "member taken")
                return
            except ValueError as exc:
                await websocket.send(encode_error("bad_payload", str(exc)))
                return
            await world.welcome(session)

            recv_task = asyncio.create_task(self._recv_loop(websocket, world, session))
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
                await world.detach(session)

    async def _recv_loop(self, websocket: ServerConnection, world: World, session) -> None:
        try:
            async for message in websocket:
                if not isinstance(message, str):
                    message = message.decode("utf-8")
                try:
                    parsed = ClientMessage.from_json(message)
                except Exception as exc:
                    world.push_error(session, "bad_payload", str(exc))
                    continue
                if parsed.type == "hello":
                    world.push_error(session, "bad_payload", "already greeted")
                    continue
                await world.handle(session, parsed.type, parsed.payload)
        except ConnectionClosed:
            return
