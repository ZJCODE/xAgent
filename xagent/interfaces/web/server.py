"""Local host process for the built-in browser web client."""

from __future__ import annotations

import logging
import threading
import webbrowser
from ipaddress import ip_address
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI

from ..cli.agents import management_root
from .agent_routes import register_agent_session_routes
from .channel_routes import register_channel_routes
from .proxy import register_runtime_bridge
from .session import WebAgentSession
from .spa import register_spa_routes

_STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def normalize_loopback_host(host: str) -> str:
    normalized = str(host or "").strip()
    if normalized.lower() == "localhost":
        return "localhost"
    candidate = normalized[1:-1] if normalized.startswith("[") and normalized.endswith("]") else normalized
    try:
        address = ip_address(candidate)
    except ValueError as exc:
        raise ValueError("Web UI host must be localhost, 127.0.0.1, or ::1") from exc
    if not address.is_loopback:
        raise ValueError(
            "Web UI is unauthenticated and may only bind to a loopback address"
        )
    return candidate


class WebClientServer:
    """Serve the SPA and bridge it to the selected Agent Runtime."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        config_dir: Optional[str] = None,
        initial_agent: Optional[str] = None,
        static_dir: Optional[Path] = None,
        registry_root: Optional[Path] = None,
    ) -> None:
        self.host = normalize_loopback_host(host)
        self.port = int(port)
        if isinstance(port, bool) or not 1 <= self.port <= 65_535:
            raise ValueError("Web UI port must be between 1 and 65535")
        self.static_dir = static_dir or _STATIC_DIR
        index_path = self.static_dir / "index.html"
        if not index_path.is_file():
            raise FileNotFoundError(
                f"Web client UI assets missing at {self.static_dir}. "
                "Reinstall the backend with: pip install --user --force-reinstall myxagent"
            )
        self.logger = logging.getLogger(self.__class__.__name__)
        self.session = WebAgentSession(
            initial_config_dir=Path(config_dir).expanduser().resolve() if config_dir else management_root(),
            initial_agent_name=initial_agent,
            registry_root=registry_root,
        )
        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        app = FastAPI(
            title="xAgent Web Client",
            description="Browser client for local xAgent Runtime control",
            version="1.0.0",
        )
        register_agent_session_routes(app, self.session)
        register_channel_routes(app, self.session)
        register_runtime_bridge(
            app,
            resolve_config_dir=self.session.get_current_config_dir,
            logger=self.logger,
        )
        register_spa_routes(app, static_dir=self.static_dir, logger=self.logger)
        return app

    def run(self, *, open_browser: bool = False) -> None:
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self.logger.info("Starting xAgent Web Client on %s:%s", self.host, self.port)
        if open_browser:
            browse_host = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            if ":" in browse_host and not browse_host.startswith("["):
                browse_host = f"[{browse_host}]"
            url = f"http://{browse_host}:{self.port}"
            opener = threading.Timer(1.0, lambda: webbrowser.open(url))
            opener.daemon = True
            opener.start()

        uvicorn.run(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
        )
