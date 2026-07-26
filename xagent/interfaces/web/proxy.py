"""Web bridge to the authenticated loopback Runtime control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from ...core.runtime import (
    RuntimeClient,
    RuntimeLaunchError,
    RuntimeLauncher,
    RuntimeUnavailable,
)
from ...core.runtime.types import LOCAL_OWNER_PERSON_ID
from ..server.models import (
    MAX_INPUT_ITEMS,
    MAX_INPUT_TEXT_CHARS,
    ChatAttachmentInput,
    ChatImageInput,
    ObserveInput,
)


class WebChatInput(BaseModel):
    """Owner-only chat payload for the local Web control surface."""

    model_config = ConfigDict(extra="forbid")

    user_message: str = Field(max_length=MAX_INPUT_TEXT_CHARS)
    image_source: str | list[str] | None = None
    images: list[ChatImageInput] | None = Field(
        default=None,
        max_length=MAX_INPUT_ITEMS,
    )
    attachments: list[ChatAttachmentInput] | None = Field(
        default=None,
        max_length=MAX_INPUT_ITEMS,
    )
    stream: bool = False


def register_runtime_bridge(
    app: FastAPI,
    *,
    resolve_config_dir: Callable[[], Path],
    logger: logging.Logger | None = None,
) -> None:
    """Expose browser-safe routes without exposing the Runtime bearer token."""
    log = logger or logging.getLogger(__name__)

    async def client(*, start: bool = False) -> RuntimeClient:
        config_dir = resolve_config_dir().expanduser().resolve()
        runtime_client = RuntimeClient(config_dir)
        try:
            await asyncio.to_thread(runtime_client.status)
        except RuntimeUnavailable:
            if not start:
                raise
            try:
                await asyncio.to_thread(RuntimeLauncher(config_dir).start)
            except RuntimeLaunchError as exc:
                raise RuntimeUnavailable(str(exc)) from exc
        return runtime_client

    async def request(
        method: str,
        path: str,
        *,
        start: bool = False,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        try:
            runtime_client = await client(start=start)
            kwargs = {"json": payload} if payload is not None else {}
            return await asyncio.to_thread(
                runtime_client.request,
                method,
                path,
                **kwargs,
            )
        except RuntimeUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/health", tags=["Health"])
    async def health():
        try:
            status = await request("GET", "/v1/runtime")
        except HTTPException:
            return {"status": "stopped", "service": "xAgent Runtime"}
        return {
            "status": "healthy" if status.get("running") else "stopped",
            "service": "xAgent Runtime",
        }

    @app.post("/chat")
    async def chat(input_data: WebChatInput):
        payload = _chat_event_payload(input_data)
        payload["wait"] = True
        result = await request("POST", "/v1/events", start=True, payload=payload)
        return {"reply": _final_content(result.get("result") or {})}

    @app.post("/observe")
    async def observe(input_data: ObserveInput):
        payload = _observe_event_payload(input_data)
        payload["wait"] = True
        result = await request("POST", "/v1/events", start=True, payload=payload)
        return result.get("result") or {}

    @app.websocket("/ws/chat")
    async def websocket_chat(websocket: WebSocket):
        await websocket.accept()
        try:
            raw = await websocket.receive_json()
            input_data = WebChatInput.model_validate(raw)
            await _stream_control_event(
                websocket,
                await client(start=True),
                _chat_event_payload(input_data),
            )
        except WebSocketDisconnect:
            return
        except Exception as exc:
            log.warning("Web Runtime chat failed: %s", exc)
            await _send_websocket_error(websocket, exc)

    @app.websocket("/ws/observe")
    async def websocket_observe(websocket: WebSocket):
        await websocket.accept()
        try:
            raw = await websocket.receive_json()
            input_data = ObserveInput.model_validate(raw)
            await _stream_control_event(
                websocket,
                await client(start=True),
                _observe_event_payload(input_data),
            )
        except WebSocketDisconnect:
            return
        except Exception as exc:
            log.warning("Web Runtime observation failed: %s", exc)
            await _send_websocket_error(websocket, exc)

    @app.get("/api/runtime")
    async def runtime_status():
        try:
            return await request("GET", "/v1/runtime")
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            return {
                "pid": None,
                "instance_id": "",
                "started_at": None,
                "running": False,
                "channels": [],
            }

    @app.post("/api/runtime/{action}")
    async def runtime_action(action: str):
        if action not in {"start", "stop", "restart"}:
            raise HTTPException(status_code=404, detail="unknown Runtime action")
        config_dir = resolve_config_dir().expanduser().resolve()
        launcher = RuntimeLauncher(config_dir)
        try:
            if action == "start":
                outcome = await asyncio.to_thread(launcher.start)
            elif action == "stop":
                outcome = await asyncio.to_thread(launcher.stop)
            else:
                outcome = await asyncio.to_thread(launcher.restart)
        except RuntimeLaunchError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        status = await asyncio.to_thread(launcher.status)
        return {
            "action": action,
            "outcome": outcome.state,
            "runtime": status
            or {
                "pid": None,
                "instance_id": "",
                "started_at": None,
                "running": False,
                "channels": [],
            },
        }

    @app.get("/api/overview")
    async def runtime_overview():
        try:
            return await request("GET", "/v1/overview")
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            return {
                "runtime": {
                    "pid": None,
                    "instance_id": "",
                    "started_at": None,
                    "uptime_seconds": 0,
                    "running": False,
                    "channels": [],
                },
                "counts": None,
                "recent_events": [],
                "recent_memory": [],
            }

    @app.get("/api/messages")
    async def list_messages(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0, le=100_000),
        q: str = Query(default="", max_length=256),
        role: str = Query(default="", max_length=32),
        source: str = Query(default="", max_length=64),
    ):
        query = urlencode(
            {
                "limit": limit,
                "offset": offset,
                "q": q,
                "role": role,
                "source": source,
            }
        )
        return await request("GET", f"/v1/messages?{query}")

    @app.get("/api/memory")
    async def list_memory(
        scope: str = Query(default="all", max_length=16),
        q: str = Query(default="", max_length=256),
        limit: int = Query(default=200, ge=1, le=500),
    ):
        query = urlencode({"scope": scope, "q": q, "limit": limit})
        return await request("GET", f"/v1/memory?{query}")

    @app.get("/api/memory/file")
    async def read_memory_file(
        path: str = Query(min_length=1, max_length=512),
    ):
        return await request("GET", f"/v1/memory/file?{urlencode({'path': path})}")

    @app.get("/api/tasks")
    async def list_tasks(status: str | None = None):
        suffix = f"?status={status}" if status else ""
        return await request("GET", f"/v1/tasks{suffix}")

    @app.post("/api/tasks", status_code=201)
    async def create_task(request_: Request):
        payload = await request_.json()
        payload["created_source"] = "web"
        payload["created_by"] = LOCAL_OWNER_PERSON_ID
        return await request(
            "POST",
            "/v1/tasks",
            start=True,
            payload=payload,
        )

    @app.patch("/api/tasks/{task_id}")
    async def update_task(task_id: str, request_: Request):
        return await request(
            "PATCH",
            f"/v1/tasks/{task_id}",
            payload=await request_.json(),
        )

    @app.post("/api/tasks/{task_id}/{action}")
    async def change_task(task_id: str, action: str):
        if action not in {"pause", "resume"}:
            raise HTTPException(status_code=404, detail="unknown task action")
        return await request("POST", f"/v1/tasks/{task_id}/{action}")

    @app.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        return await request("DELETE", f"/v1/tasks/{task_id}")

    @app.get("/api/deliveries")
    async def list_deliveries(status: str | None = None):
        suffix = f"?status={status}" if status else ""
        return await request("GET", f"/v1/deliveries{suffix}")

    @app.post("/api/deliveries/{delivery_id}/retry")
    async def retry_delivery(delivery_id: str):
        return await request("POST", f"/v1/deliveries/{delivery_id}/retry")

async def _stream_control_event(
    websocket: WebSocket,
    client: RuntimeClient,
    payload: dict[str, Any],
) -> None:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - packaging guard
        raise RuntimeError("WebSocket support is missing from the xAgent installation") from exc

    info = client.info()
    control_url = str(info["control_url"])
    target = (
        control_url.replace("http://", "ws://", 1)
        + f"/v1/events/stream?token={info['token']}"
    )
    async with websockets.connect(target, proxy=None) as upstream:
        await upstream.send(json.dumps(payload, ensure_ascii=False))
        async for message in upstream:
            data = json.loads(message)
            await websocket.send_json(data)
            if data.get("type") == "done":
                break
    await websocket.close()


async def _send_websocket_error(websocket: WebSocket, exc: Exception) -> None:
    try:
        await websocket.send_json({"type": "error", "error": str(exc)})
        await websocket.send_json({"type": "done"})
        await websocket.close()
    except RuntimeError:
        pass


def _chat_event_payload(input_data: WebChatInput) -> dict[str, Any]:
    attachments = [
        item.model_dump(mode="json", exclude_none=True)
        for item in (input_data.attachments or [])
    ]
    image_source = input_data.image_source
    if image_source is None and input_data.images:
        image_source = [
            str(item.blob_url or item.external_url or item.workspace_path or "")
            for item in input_data.images
            if item.blob_url or item.external_url or item.workspace_path
        ]
    return {
        "event_id": uuid.uuid4().hex,
        "kind": "chat",
        "source": "web",
        "conversation_id": "web:main",
        "speaker_id": LOCAL_OWNER_PERSON_ID,
        "audience_ids": [LOCAL_OWNER_PERSON_ID],
        "content": input_data.user_message,
        "metadata": {
            "stream": bool(getattr(input_data, "stream", False)),
            "attachments": attachments,
            "image_source": image_source,
        },
    }


def _observe_event_payload(input_data: ObserveInput) -> dict[str, Any]:
    source = str(input_data.source or "web")
    return {
        "event_id": uuid.uuid4().hex,
        "kind": "observe",
        "source": "web",
        "conversation_id": "web:main",
        "speaker_id": LOCAL_OWNER_PERSON_ID,
        "audience_ids": [],
        "content": input_data.context,
        "metadata": {
            "source": source,
            "event_type": input_data.event_type or "observation",
            "observation_metadata": input_data.metadata,
        },
    }


def _final_content(result: dict[str, Any]) -> str:
    final = ""
    for event in result.get("events", []):
        if event.get("type") == "message_done":
            final = str(event.get("content") or final)
    return final
