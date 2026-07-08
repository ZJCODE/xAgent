"""Local host process for the built-in browser web client."""

from __future__ import annotations

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI

from ...server.admin_routes import register_admin_routes
from .agent_routes import register_agent_session_routes
from .channel_routes import register_channel_routes
from .proxy import register_api_proxy
from .session import WebAgentSession
from .spa import register_spa_routes

_STATIC_DIR = Path(__file__).resolve().parents[2] / "static"


class WebClientServer:
    """Serve the SPA, admin data for every registered agent, and proxy chat
    traffic to whichever agent's api channel is currently selected."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        api_url: str,
        config_dir: Optional[str] = None,
        initial_agent: Optional[str] = None,
        static_dir: Optional[Path] = None,
        registry_root: Optional[Path] = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.api_url = api_url.rstrip("/")
        self.static_dir = static_dir or _STATIC_DIR
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = WebAgentSession(
            initial_config_dir=Path(config_dir).expanduser().resolve() if config_dir else Path.cwd(),
            initial_agent_name=initial_agent,
            initial_api_url=self.api_url,
            registry_root=registry_root,
        )
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent Web Client",
            description="Browser client for the xAgent api channel",
            version="1.0.0",
        )
        register_agent_session_routes(app, self.session)
        register_channel_routes(app, self.session.get_current_config_dir)
        register_admin_routes(app, self.session.get_current_admin)
        register_api_proxy(app, resolve_api_url=self.session.get_current_api_url, logger=self.logger)
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
