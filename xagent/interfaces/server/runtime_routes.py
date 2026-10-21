"""Runtime chat and observe routes for the HTTP server."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError
from pydantic import BaseModel

from .models import AgentInput, ChatInput, ObserveInput
from .serializers import response_payload
from ...integrations.api.chat_relay import ChatPayloadError, relay_chat_socket
from ...integrations.api.constants import CLIENT_HTTP, CLIENT_WS


class StopInput(BaseModel):
    turn_id: str


class SteerInput(BaseModel):
    turn_id: str
    content: str = ""
    user_id: str | None = None
    room_name: str | None = None

if TYPE_CHECKING:
    from ...integrations.api.adapter import ApiChannelAdapter


def register_runtime_routes(app: FastAPI, adapter: "ApiChannelAdapter") -> None:
    @app.get("/i/health", tags=["Health"])
    async def health_check():
        return "ok"

    @app.get("/health")
    async def health():
        return {"status": "healthy", "service": "xAgent HTTP Server"}

    @app.post("/chat")
    async def chat(input_data: ChatInput):
        adapter.logger.info("Chat request from %s", input_data.user_id)
        try:
            return await adapter.chat.run_chat_response(input_data, client=CLIENT_HTTP)
        except HTTPException:
            raise
        except Exception as exc:
            adapter.logger.error("Agent processing error for %s: %s", input_data.user_id, exc)
            raise HTTPException(status_code=500, detail=f"Agent processing error: {str(exc)}")

    @app.post("/chat/stop")
    async def stop_chat(input_data: StopInput):
        if not input_data.turn_id:
            raise HTTPException(422, "turn_id is required")
        result = adapter.chat.abort_turn(input_data.turn_id)
        adapter.logger.info("Chat stop requested: stopped=%s", result.get("stopped"))
        return result

    @app.post("/chat/steer")
    async def steer_chat(input_data: SteerInput):
        if not input_data.turn_id:
            raise HTTPException(422, "turn_id is required")
        result = await adapter.chat.steer_turn(
            turn_id=input_data.turn_id,
            content=input_data.content,
            user_id=input_data.user_id,
            room_name=input_data.room_name,
        )
        adapter.logger.info("Chat steer requested: steered=%s", result.get("steered"))
        return result

    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket):
        await websocket.accept()
        adapter.logger.info("WebSocket chat connected")

        def start_stream(payload: dict):
            if not isinstance(payload, dict):
                raise ChatPayloadError("Invalid chat payload.", status_code=422)
            try:
                input_data = AgentInput.model_validate(payload)
            except ValidationError as exc:
                raise ChatPayloadError(
                    "Invalid chat payload.",
                    status_code=422,
                    details=exc.errors(),
                ) from exc
            adapter.logger.info(
                "WebSocket chat request from %s, stream=%s",
                input_data.user_id,
                input_data.stream,
            )
            return adapter.chat.chat_event_stream(input_data, client=CLIENT_WS)

        try:
            await relay_chat_socket(websocket, start_stream)
        except WebSocketDisconnect:
            adapter.logger.info("WebSocket chat disconnected")
        except Exception as exc:
            adapter.logger.error("Unexpected WebSocket chat error: %s", exc)
            await adapter.chat.send_websocket_error(
                websocket,
                f"Agent processing error: {str(exc)}",
            )

    @app.websocket("/ws/observe")
    async def websocket_observe(websocket: WebSocket):
        await websocket.accept()
        adapter.logger.info("WebSocket observe connected")

        while True:
            try:
                raw_payload = await websocket.receive_json()
                input_data = ObserveInput.model_validate(raw_payload)
                adapter.logger.info(
                    "WebSocket observe request: source=%s, type=%s",
                    input_data.source,
                    input_data.event_type,
                )
                await adapter.chat.send_websocket_observe_events(websocket, input_data)
            except WebSocketDisconnect:
                adapter.logger.info("WebSocket observe disconnected")
                break
            except json.JSONDecodeError as exc:
                adapter.logger.warning("Invalid WebSocket observe JSON: %s", exc)
                await adapter.chat.send_websocket_error(
                    websocket,
                    "Invalid JSON payload.",
                    status_code=400,
                    details=str(exc),
                )
            except ValidationError as exc:
                adapter.logger.warning("Invalid WebSocket observe payload: %s", exc)
                await adapter.chat.send_websocket_error(
                    websocket,
                    "Invalid observe payload.",
                    status_code=422,
                    details=exc.errors(),
                )
            except Exception as exc:
                adapter.logger.error("Unexpected WebSocket observe error: %s", exc)
                await adapter.chat.send_websocket_error(
                    websocket,
                    f"Agent observe error: {str(exc)}",
                )

    @app.websocket("/ws/tasks")
    async def websocket_tasks(websocket: WebSocket):
        user_id = (websocket.query_params.get("user_id") or "web_user").strip() or "web_user"
        await websocket.accept()
        await adapter.delivery.register_subscriber(user_id, websocket)
        adapter.logger.info("Scheduled task WebSocket connected for %s", user_id)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            adapter.logger.info("Scheduled task WebSocket disconnected for %s", user_id)
        finally:
            await adapter.delivery.unregister_subscriber(user_id, websocket)

    @app.post("/observe")
    async def observe(input_data: ObserveInput):
        adapter.logger.info(
            "Observation request: source=%s, type=%s",
            input_data.source,
            input_data.event_type,
        )
        try:
            response = await adapter.chat.run_observe(input_data)
            return response_payload(response)
        except HTTPException:
            raise
        except Exception as exc:
            adapter.logger.error(
                "Agent observe error: source=%s type=%s error=%s",
                input_data.source,
                input_data.event_type,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"Agent observe error: {str(exc)}")
