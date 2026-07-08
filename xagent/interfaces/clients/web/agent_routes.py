"""Multi-agent listing/selection routes for the built-in web client."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ...cli.agents import AgentRegistryError
from .session import WebAgentSession


class SelectAgentInput(BaseModel):
    name: str


def register_agent_session_routes(app: FastAPI, session: WebAgentSession) -> None:
    @app.get("/api/agents", tags=["Agents"])
    async def list_agents():
        return session.snapshot()

    @app.post("/api/agents/select", tags=["Agents"])
    async def select_agent(input_data: SelectAgentInput):
        try:
            return session.select(input_data.name)
        except AgentRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
