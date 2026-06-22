"""Runtime chat and observe routes for the HTTP server."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from ..dto.models import AgentInput, ChatInput, ObserveInput
from ..serializers import response_payload

if TYPE_CHECKING:
    from ..app import AgentHTTPServer


def register_runtime_routes(app: FastAPI, server: "AgentHTTPServer") -> None:
    @app.get("/i/health", tags=["Health"])
    async def health_check():
        return "ok"

    @app.get("/health")
    async def health():
        return {"status": "healthy", "service": "xAgent HTTP Server"}

    @app.post("/chat")
    async def chat(input_data: ChatInput):
        server.logger.info("Chat request from %s", input_data.user_id)
        try:
            response = await server._run_chat_with_limits(input_data)
            return {"reply": response_payload(response)}
        except HTTPException:
            raise
        except Exception as exc:
            server.logger.error("Agent processing error for %s: %s", input_data.user_id, exc)
            raise HTTPException(status_code=500, detail=f"Agent processing error: {str(exc)}")

    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket):
        await websocket.accept()
        server.logger.info("WebSocket chat connected")

        while True:
            try:
                raw_payload = await websocket.receive_json()
                input_data = AgentInput.model_validate(raw_payload)
                server.logger.info(
                    "WebSocket chat request from %s, stream=%s",
                    input_data.user_id,
                    input_data.stream,
                )
                await server._send_websocket_chat_events(websocket, input_data)
            except WebSocketDisconnect:
                server.logger.info("WebSocket chat disconnected")
                break
            except json.JSONDecodeError as exc:
                server.logger.warning("Invalid WebSocket chat JSON: %s", exc)
                await server._send_websocket_error(
                    websocket,
                    "Invalid JSON payload.",
                    status_code=400,
                    details=str(exc),
                )
            except ValidationError as exc:
                server.logger.warning("Invalid WebSocket chat payload: %s", exc)
                await server._send_websocket_error(
                    websocket,
                    "Invalid chat payload.",
                    status_code=422,
                    details=exc.errors(),
                )
            except Exception as exc:
                server.logger.error("Unexpected WebSocket chat error: %s", exc)
                await server._send_websocket_error(
                    websocket,
                    f"Agent processing error: {str(exc)}",
                )

    @app.websocket("/ws/observe")
    async def websocket_observe(websocket: WebSocket):
        await websocket.accept()
        server.logger.info("WebSocket observe connected")

        while True:
            try:
                raw_payload = await websocket.receive_json()
                input_data = ObserveInput.model_validate(raw_payload)
                server.logger.info(
                    "WebSocket observe request: source=%s, type=%s",
                    input_data.source,
                    input_data.event_type,
                )
                await server._send_websocket_observe_events(websocket, input_data)
            except WebSocketDisconnect:
                server.logger.info("WebSocket observe disconnected")
                break
            except json.JSONDecodeError as exc:
                server.logger.warning("Invalid WebSocket observe JSON: %s", exc)
                await server._send_websocket_error(
                    websocket,
                    "Invalid JSON payload.",
                    status_code=400,
                    details=str(exc),
                )
            except ValidationError as exc:
                server.logger.warning("Invalid WebSocket observe payload: %s", exc)
                await server._send_websocket_error(
                    websocket,
                    "Invalid observe payload.",
                    status_code=422,
                    details=exc.errors(),
                )
            except Exception as exc:
                server.logger.error("Unexpected WebSocket observe error: %s", exc)
                await server._send_websocket_error(
                    websocket,
                    f"Agent observe error: {str(exc)}",
                )

    @app.websocket("/ws/tasks")
    async def websocket_tasks(websocket: WebSocket):
        user_id = (websocket.query_params.get("user_id") or "web_user").strip() or "web_user"
        await websocket.accept()
        await server._register_task_subscriber(user_id, websocket)
        server.logger.info("Scheduled task WebSocket connected for %s", user_id)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            server.logger.info("Scheduled task WebSocket disconnected for %s", user_id)
        finally:
            await server._unregister_task_subscriber(user_id, websocket)

    @app.post("/observe")
    async def observe(input_data: ObserveInput):
        server.logger.info(
            "Observation request: source=%s, type=%s",
            input_data.source,
            input_data.event_type,
        )
        try:
            response = await server._run_observe_with_limits(input_data)
            return response_payload(response)
        except HTTPException:
            raise
        except Exception as exc:
            server.logger.error(
                "Agent observe error: source=%s type=%s error=%s",
                input_data.source,
                input_data.event_type,
                exc,
            )
            raise HTTPException(status_code=500, detail=f"Agent observe error: {str(exc)}")

    @app.post("/clear_messages")
    async def clear_messages():
        server.logger.info("Clear messages request")
        try:
            await server.message_storage.clear_messages()
            return {
                "status": "success",
                "message": "Message stream cleared",
            }
        except Exception as exc:
            server.logger.error("Failed to clear messages: %s", exc)
            raise HTTPException(status_code=500, detail=f"Failed to clear messages: {str(exc)}")
