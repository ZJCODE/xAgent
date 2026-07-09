"""Configuration and process paths for the built-in browser web client."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

from ..base import BaseAgentConfig
from .agents import management_root
from .channels import api_config, load_config_file
from .processes import ManagedProcessPaths


DEFAULT_WEB_CLIENT_PORT = 1415


def web_client_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized global web client settings merged with API defaults."""
    web_cfg = config.get("web") if isinstance(config, Mapping) else None
    web_cfg = dict(web_cfg) if isinstance(web_cfg, Mapping) else {}

    api_cfg = api_config(config)
    api_url = str(web_cfg.get("api_url") or "").strip() or _default_api_url(api_cfg)

    return {
        "enabled": bool(web_cfg.get("enabled", True)),
        "host": BaseAgentConfig.DEFAULT_HOST,
        "port": DEFAULT_WEB_CLIENT_PORT,
        "api_url": api_url.rstrip("/"),
    }


def _default_api_url(api_cfg: Mapping[str, Any]) -> str:
    host = str(api_cfg.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = api_cfg.get("port")
    if port is None:
        port = BaseAgentConfig.DEFAULT_PORT
    browse_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"


def web_client_runtime_root() -> Path:
    """Return the global runtime root for the web client process."""
    return management_root()


def web_client_paths(*, root: Optional[Path] = None) -> ManagedProcessPaths:
    """Return global PID and log paths for the managed web client process."""
    runtime_root = (root or web_client_runtime_root()).expanduser().resolve()
    return ManagedProcessPaths(
        pid_path=runtime_root / "run" / "web.pid",
        log_path=runtime_root / "logs" / "web.log",
    )


def web_client_public_url(config: Mapping[str, Any]) -> str:
    """Return the browser-facing URL for the web client."""
    web_cfg = web_client_config(config)
    host = str(web_cfg["host"])
    port = int(web_cfg["port"])
    browse_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"


def api_url_to_ws_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return parsed._replace(scheme=scheme).geturl()


__all__ = [
    "DEFAULT_WEB_CLIENT_PORT",
    "api_url_to_ws_url",
    "load_config_file",
    "web_client_config",
    "web_client_paths",
    "web_client_public_url",
    "web_client_runtime_root",
]
