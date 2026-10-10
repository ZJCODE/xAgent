"""Route live mutations through the owner and lock offline mutations."""
from __future__ import annotations

import httpx
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from ...core.runtime.client import RuntimeClient
from ...core.runtime.ownership import RuntimeOwnership, runtime_is_active

ADMIN_PREFIXES = (
    "/api/agent/", "/api/tasks", "/api/memory/", "/api/messages",
    "/api/workspace/", "/api/skills/",
)


def register_admin_access(app: FastAPI, resolve_admin) -> None:
    if getattr(app.state, "admin_access_registered", False):
        return
    app.state.admin_access_registered = True

    @app.middleware("http")
    async def owner_mutations(request: Request, call_next):
        path = request.url.path
        mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"}
        admin_path = path.startswith(ADMIN_PREFIXES) or path == "/clear_messages" or (path.startswith("/api/channels/") and path.endswith("/setup"))
        if not mutation or not admin_path:
            return await call_next(request)
        server = resolve_admin()
        if getattr(server, "is_runtime_owner", False):
            # Clearing state while an active turn or diary commit uses it is
            # rejected, rather than letting two operations corrupt each other.
            if path.startswith("/api/memory/") or path == "/clear_messages":
                if getattr(getattr(server.agent, "inbox", None), "busy", False):
                    return JSONResponse({"detail": "Wait for the current Agent turn before clearing state."}, status_code=409)
                handler = getattr(server.agent, "memory_handler", None)
                if handler is not None:
                    async with handler._maintenance_guard(refresh_state=True):
                        response = await call_next(request)
                        if response.status_code < 400 and path == "/api/memory/clear" and request.query_params.get("scope", "all") == "all":
                            handler._last_processed_message_id = 0
                        return response
            return await call_next(request)
        root = server.config_dir
        if runtime_is_active(root):
            try:
                upstream = await RuntimeClient(root).request(
                    request.method, path, params=list(request.query_params.multi_items()),
                    content=await request.body(),
                    headers={"content-type": request.headers.get("content-type", "application/json")},
                )
                return Response(upstream.content, status_code=upstream.status_code,
                                media_type=upstream.headers.get("content-type"))
            except httpx.HTTPStatusError as exc:
                return Response(exc.response.content, status_code=exc.response.status_code, media_type="application/json")
            except Exception as exc:
                return JSONResponse({"detail": f"Agent runtime is unavailable: {exc}"}, status_code=503)
        ownership = RuntimeOwnership(root)
        try:
            ownership.acquire()
        except Exception as exc:
            return JSONResponse({"detail": f"Agent runtime acquired ownership; retry this operation: {exc}"}, status_code=409)
        try:
            return await call_next(request)
        finally:
            ownership.release()
