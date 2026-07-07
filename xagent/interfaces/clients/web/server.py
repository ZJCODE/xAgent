"""Local host process for the built-in browser web client."""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI

from .proxy import register_api_proxy
from .spa import register_spa_routes

_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


class WebClientServer:
    """Serve the SPA and proxy API/WebSocket traffic to the api channel."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        api_url: str,
        static_dir: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.api_url = api_url.rstrip("/")
        self.static_dir = static_dir or _STATIC_DIR
        self.logger = logging.getLogger(self.__class__.__name__)
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent Web Client",
            description="Browser client for the xAgent api channel",
            version="1.0.0",
        )
        register_api_proxy(app, api_url=self.api_url, logger=self.logger)
        register_spa_routes(app, static_dir=self.static_dir, logger=self.logger)
        return app

    def run(self, *, open_browser: bool = False) -> None:
        self.logger.info("Starting xAgent Web Client on %s:%s", self.host, self.port)
        self.logger.info("Proxying to api channel at %s", self.api_url)

        if open_browser:
            browse_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            if ":" in browse_host and not browse_host.startswith("["):
                browse_host = f"[{browse_host}]"
            url = f"http://{browse_host}:{self.port}"
            threading.Timer(1.0, lambda: webbrowser.open(url)).start()

        uvicorn.run(self.app, host=self.host, port=self.port)
