"""Multi-agent listing/selection routes for the built-in web client."""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from ...cli.agents import AgentRegistryError
from .session import WebAgentSession


class SelectAgentInput(BaseModel):
    name: str


class InitSelectionInput(BaseModel):
    provider: str = "openai"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    identity: str = ""
    model_api: str = ""
    supports_vision: bool = False
    search_provider: str = "none"
    search_api_key: str = ""
    image_generation_provider: str = "none"
    image_generation_api_key: str = ""
    observability_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""
    voice_enabled: bool = False
    voice_provider: str = "none"
    voice_api_key: str = ""
    voice_stt_provider: str = ""
    voice_stt_api_key: str = ""
    voice_tts_provider: str = ""
    voice_tts_api_key: str = ""
    voice_enable_interruptions: bool = False
    voice_wake_enabled: bool = False
    voice_wake_phrases: list[str] = Field(default_factory=list)
    voice_exit_phrases: list[str] = Field(default_factory=list)


class CreateAgentInput(BaseModel):
    name: str
    title: Optional[str] = None
    replace_existing: bool = False
    selection: InitSelectionInput


class DeleteAgentInput(BaseModel):
    confirm: str


def register_agent_session_routes(app: FastAPI, session: WebAgentSession) -> None:
    @app.get("/api/agents", tags=["Agents"])
    async def list_agents():
        return session.snapshot()

    @app.get("/api/agents/setup-schema", tags=["Agents"])
    async def agent_setup_schema():
        return session.setup_schema()

    @app.get("/api/agents/availability", tags=["Agents"])
    async def agent_name_availability(name: str):
        try:
            return session.check_name_availability(name)
        except AgentRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/agents/select", tags=["Agents"])
    async def select_agent(input_data: SelectAgentInput):
        try:
            return session.select(input_data.name)
        except AgentRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/agents", tags=["Agents"])
    async def create_agent(input_data: CreateAgentInput):
        try:
            selection_data: dict[str, Any] = input_data.selection.model_dump()
            return session.create_agent(
                name=input_data.name,
                title=input_data.title,
                replace_existing=input_data.replace_existing,
                selection_data=selection_data,
            )
        except AgentRegistryError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/agents/{name}", tags=["Agents"])
    async def delete_agent(name: str, input_data: DeleteAgentInput):
        try:
            return session.delete_agent(name, confirm=input_data.confirm)
        except AgentRegistryError as exc:
            message = str(exc)
            status_code = 409 if "Failed to stop" in message else 400
            raise HTTPException(status_code=status_code, detail=message) from exc
