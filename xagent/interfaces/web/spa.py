"""SPA static routes for the web client."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def register_spa_routes(app: FastAPI, *, static_dir: Path | None = None, logger: logging.Logger | None = None) -> None:
    """Mount the built-in SPA and client-side routes."""
    logger = logger or logging.getLogger(__name__)
    static_dir = static_dir or _STATIC_DIR
    if not static_dir.is_dir():
        logger.warning("Static directory not found at %s", static_dir)
        return

    async def serve_spa_index():
        index = static_dir / "index.html"
        if index.exists():
            return FileResponse(str(index), media_type="text/html")
        raise HTTPException(status_code=404, detail="Web client UI not found")

    for route in ("/", "/memory", "/workspace", "/message", "/agent", "/skills", "/tasks", "/channels"):
        app.add_api_route(route, serve_spa_index, methods=["GET"], include_in_schema=False)

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    logger.info("Web client UI available at /")
