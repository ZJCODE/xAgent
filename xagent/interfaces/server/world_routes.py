"""Agent API routes so a running mind can enter or leave an agents-world world."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict

from ...integrations.world import WorldInhabitant
from ...integrations.world.presence import mark_world_left, mark_world_presence

if TYPE_CHECKING:
    from .app import AgentHTTPServer


class WorldJoinInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    world_url: str
    member_id: Optional[str] = None
    display_name: Optional[str] = None


def register_world_routes(app: FastAPI, server: "AgentHTTPServer") -> None:
    @app.get("/world/status")
    async def world_status():
        inhabitant = getattr(server, "world_inhabitant", None)
        if inhabitant is None:
            return {"connected": False}
        return inhabitant.status()

    @app.post("/world/join")
    async def world_join(input_data: WorldJoinInput):
        world_url = str(input_data.world_url or "").strip()
        if not world_url:
            raise HTTPException(status_code=400, detail="world_url is required")
        member_id = str(input_data.member_id or "").strip() or Path(server.config_dir).name
        display_name = str(input_data.display_name or "").strip() or member_id
        inhabitant = getattr(server, "world_inhabitant", None)
        if inhabitant is None or inhabitant.member_id != member_id:
            inhabitant = WorldInhabitant(
                server.agent,
                member_id=member_id,
                display_name=display_name,
                logger=server.logger,
            )
            server.world_inhabitant = inhabitant
        else:
            inhabitant.display_name = display_name
        status = await inhabitant.join(world_url=world_url)
        mark_world_presence(
            server.config_dir,
            world_url=world_url,
            member_id=member_id,
            display_name=display_name,
            world_id=str(status.get("world_id") or ""),
            want_present=True,
        )
        return status

    @app.post("/world/leave")
    async def world_leave():
        inhabitant = getattr(server, "world_inhabitant", None)
        mark_world_left(server.config_dir)
        if inhabitant is None:
            return {"connected": False}
        return await inhabitant.leave()
