"""Reverse proxy from the web client to the currently selected agent's api channel."""

from __future__ import annotations

import asyncio
import logging
import json
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.responses import JSONResponse, Response

from ..cli.web_client import api_url_to_ws_url

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


def register_api_proxy(
    app: FastAPI,
    *,
    resolve_api_url: Callable[[], str],
    resolve_runtime_root: Callable[[], Path | None] | None = None,
    logger: logging.Logger | None = None,
) -> None:
    """Forward chat/observe/health traffic to whichever agent is currently selected.

    ``/api/*`` and ``/clear_messages`` are intentionally NOT proxied here — they
    are served locally by the admin routes mounted directly on the web client,
    so those tabs work without any api channel running. Only the routes that
    require a live model/tool-executing agent are forwarded: chat, observe,
    chat stop, the scheduled-task/subconscious push socket, and health checks.
    """
    logger = logger or logging.getLogger(__name__)

    def _make_root_proxy(route_path: str):
        async def handler(request: Request):
            root = resolve_runtime_root() if resolve_runtime_root is not None else None
            if root is not None:
                return await _local_http_request(request, route_path, root)
            upstream = resolve_api_url().rstrip("/")
            return await _proxy_http_request(request, f"{upstream}{route_path}")

        return handler

    for route_path in ("/chat", "/chat/stop", "/observe"):
        app.add_api_route(
            route_path,
            _make_root_proxy(route_path),
            methods=["POST", "OPTIONS"],
            include_in_schema=False,
        )
    for route_path in ("/health", "/i/health"):
        app.add_api_route(
            route_path,
            _make_root_proxy(route_path),
            methods=["GET", "HEAD", "OPTIONS"],
            include_in_schema=False,
        )

    @app.websocket("/ws/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str):
        await websocket.accept()
        root = resolve_runtime_root() if resolve_runtime_root is not None else None
        if root is not None:
            await _local_websocket(websocket, path, root, logger)
            return
        upstream = resolve_api_url().rstrip("/")
        ws_upstream = api_url_to_ws_url(upstream)
        query = websocket.scope.get("query_string", b"").decode()
        target = urljoin(f"{ws_upstream}/", f"ws/{path}")
        if query:
            target = f"{target}?{query}"

        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency guard
            await websocket.close(code=1011, reason="websockets package is required for web client proxy")
            raise RuntimeError("websockets package is required") from exc

        try:
            try:
                upstream_connection = websockets.connect(target, proxy=None)
            except TypeError:
                upstream_connection = websockets.connect(target)
            async with upstream_connection as upstream_ws:
                await _relay_websockets(websocket, upstream_ws)
        except WebSocketDisconnect:
            logger.debug("Web client websocket disconnected")
        except Exception as exc:
            logger.warning("Web client websocket proxy error: %s", exc)
            if websocket.client_state.name == "CONNECTED":
                await websocket.close(code=1011, reason=str(exc))

    logger.info("Proxying chat/observe traffic to the currently selected agent's api channel")


def _chat_kwargs(payload: dict) -> dict:
    from ..server.models import AgentInput
    from ...integrations.api.input_normalization import input_attachments, input_image_sources
    data = AgentInput.model_validate(payload)
    attachments = input_attachments(data)
    return {
        "user_message": data.user_message, "user_id": data.user_id,
        "channel": "api", "stream": bool(data.stream), "attachments": attachments,
        "image_source": input_image_sources(data, attachments=attachments),
        "event_id": getattr(data, "event_id", None) or getattr(data, "request_id", None),
        "turn_id": getattr(data, "turn_id", None),
        "request_id": getattr(data, "request_id", None),
    }


async def _local_http_request(request: Request, path: str, root: Path) -> Response:
    from ...core.runtime.client import RuntimeClient
    from ..cli.agent_runtime import ensure_runtime
    try:
        client = RuntimeClient(root)
        if path in {"/health", "/i/health"}:
            status = await client.status()
            return JSONResponse({**status, "status": status.get("status") or status.get("state", "unknown"),
                                 "service": "xAgent Runtime"})
        payload = await request.json()
        if path == "/chat/stop":
            if not payload.get("turn_id"):
                return JSONResponse({"detail": "turn_id is required"}, status_code=422)
            return JSONResponse(await client.abort(turn_id=payload["turn_id"], channel="api"))
        client = await ensure_runtime(root)
        if path == "/observe":
            return JSONResponse(await client.observe(**payload))
        final = ""
        identifiers = {}
        async for event in client.chat_events(**_chat_kwargs(payload)):
            identifiers.update({key: event[key] for key in ("turn_id", "event_id", "request_id") if event.get(key)})
            if event.get("type") == "error":
                return JSONResponse(event, status_code=int(event.get("status_code") or 500))
            if event.get("type") == "message_done":
                final = str(event.get("content") or "")
        return JSONResponse({"reply": final, **identifiers})
    except httpx.HTTPStatusError as exc:
        return Response(exc.response.content, status_code=exc.response.status_code, media_type="application/json")
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)


async def _local_websocket(websocket: WebSocket, path: str, root: Path, logger) -> None:
    from ..cli.agent_runtime import ensure_runtime
    from ...core.runtime.client import RuntimeClient
    from ...core.runtime.ownership import runtime_is_active
    try:
        if path == "tasks":
            # Passive subscriptions must keep offline browsing model-free.
            if not runtime_is_active(root):
                await websocket.close(code=1013, reason="Agent is offline")
                return
            client = RuntimeClient(root)
            user_id = websocket.query_params.get("user_id") or "web_user"
            async with client._client(timeout=None) as transport:
                async with transport.stream("GET", "/runtime/events", params={"user_id": user_id}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line.strip():
                            event = json.loads(line)
                            if event.get("type") != "keepalive":
                                await websocket.send_json(event)
            return
        if path not in {"chat", "observe"}:
            await websocket.close(code=1008, reason="Unknown runtime stream")
            return
        client = await ensure_runtime(root)
        while True:
            payload = await websocket.receive_json()
            try:
                if path == "chat":
                    async for event in client.chat_events(**_chat_kwargs(payload)):
                        await websocket.send_json(event)
                else:
                    await websocket.send_json({"type": "result", "result": await client.observe(**payload)})
                    await websocket.send_json({"type": "done"})
            except ValueError as exc:
                await websocket.send_json({"type": "error", "error": str(exc), "status_code": 422})
                await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        return
    except Exception as exc:
        logger.warning("Local Agent stream failed: %s", exc)
        if websocket.client_state.name == "CONNECTED":
            await websocket.close(code=1011, reason="Agent runtime unavailable")


async def _proxy_http_request(request: Request, target: str) -> Response:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    body = await request.body()
    params = list(request.query_params.multi_items())

    async with httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(300.0), trust_env=False) as client:
        upstream_response = await client.request(
            request.method,
            target,
            headers=headers,
            params=params,
            content=body,
        )

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }
    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )


async def _relay_websockets(client_ws: WebSocket, upstream_ws) -> None:
    async def client_to_upstream():
        try:
            while True:
                message = await client_ws.receive()
                if message["type"] == "websocket.disconnect":
                    await upstream_ws.close()
                    break
                if message["type"] == "websocket.receive":
                    data = message.get("text")
                    if data is not None:
                        await upstream_ws.send(data)
                    else:
                        await upstream_ws.send(message.get("bytes") or b"")
        except WebSocketDisconnect:
            await upstream_ws.close()

    async def upstream_to_client():
        async for message in upstream_ws:
            if isinstance(message, bytes):
                await client_ws.send_bytes(message)
            else:
                await client_ws.send_text(message)

    tasks = [
        asyncio.create_task(client_to_upstream()),
        asyncio.create_task(upstream_to_client()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, WebSocketDisconnect):
            raise exc
