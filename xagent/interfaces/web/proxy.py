"""Reverse proxy from the web client to the currently selected agent's api channel."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable
from urllib.parse import urljoin

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from starlette.responses import Response

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
    logger: logging.Logger | None = None,
) -> None:
    """Forward chat/observe/health traffic to whichever agent is currently selected.

    ``/api/*`` and ``/clear_messages`` are intentionally NOT proxied here — they
    are served locally by the admin routes mounted directly on the web client,
    so those tabs work without any api channel running. Only the routes that
    require a live model/tool-executing agent are forwarded: chat, observe,
    the scheduled-task/subconscious push socket, and health checks.
    """
    logger = logger or logging.getLogger(__name__)

    def _make_root_proxy(route_path: str):
        async def handler(request: Request):
            upstream = resolve_api_url().rstrip("/")
            return await _proxy_http_request(request, f"{upstream}{route_path}")

        return handler

    for route_path in ("/chat", "/observe", "/health", "/i/health"):
        app.add_api_route(
            route_path,
            _make_root_proxy(route_path),
            methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
            include_in_schema=False,
        )

    @app.websocket("/ws/{path:path}")
    async def proxy_websocket(websocket: WebSocket, path: str):
        await websocket.accept()
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
    for task in done:
        exc = task.exception()
        if exc and not isinstance(exc, WebSocketDisconnect):
            raise exc
