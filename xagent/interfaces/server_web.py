"""Web UI route registration for the HTTP server."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


def register_spa_routes(app: FastAPI, *, static_dir: Path, logger: logging.Logger) -> None:
    if static_dir.is_dir():
        async def serve_spa_index():
            index = static_dir / "index.html"
            if index.exists():
                return FileResponse(str(index), media_type="text/html")
            raise HTTPException(status_code=404, detail="Web UI not found")

        @app.get("/", include_in_schema=False)
        async def serve_index():
            return await serve_spa_index()

        @app.get("/memory", include_in_schema=False)
        async def serve_memory():
            return await serve_spa_index()

        @app.get("/workspace", include_in_schema=False)
        async def serve_workspace():
            return await serve_spa_index()

        @app.get("/message", include_in_schema=False)
        async def serve_message():
            return await serve_spa_index()

        @app.get("/agent", include_in_schema=False)
        async def serve_agent():
            return await serve_spa_index()

        @app.get("/skills", include_in_schema=False)
        async def serve_skills():
            return await serve_spa_index()

        @app.get("/tasks", include_in_schema=False)
        async def serve_tasks():
            return await serve_spa_index()

        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
        logger.info("Web UI available at /")
    else:
        logger.warning("Static directory not found at %s — web UI disabled", static_dir)