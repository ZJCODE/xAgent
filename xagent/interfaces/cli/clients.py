"""Client names and configuration for xAgent UI clients.

Clients are *not* channels. They are independent applications that call into
transport channels (typically ``channels.api``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlparse

from ..base import BaseAgentConfig
from .channels import api_config, load_config_file
from .processes import ManagedProcessPaths


CLIENT_WEB = "web"
VALID_CLIENTS = {CLIENT_WEB}


class ClientSelectionError(ValueError):
    """Raised when a user provided an invalid client selection."""


def web_client_config(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return normalized ``clients.web`` settings merged with API defaults."""
    clients = config.get("clients") if isinstance(config, Mapping) else None
    web_cfg = clients.get(CLIENT_WEB) if isinstance(clients, Mapping) else None
    web_cfg = dict(web_cfg) if isinstance(web_cfg, Mapping) else {}

    api_cfg = api_config(config)
    host = str(web_cfg.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = web_cfg.get("port")
    if port is None:
        port = BaseAgentConfig.DEFAULT_PORT + 1
    api_url = str(web_cfg.get("api_url") or "").strip() or _default_api_url(api_cfg)

    return {
        "enabled": bool(web_cfg.get("enabled", True)),
        "host": host,
        "port": int(port),
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


def client_paths(config_dir: Path, client: str) -> ManagedProcessPaths:
    """Return PID and log paths for a managed client process."""
    if client not in VALID_CLIENTS:
        raise ClientSelectionError(f"Unknown client {client!r}. Expected one of: {', '.join(sorted(VALID_CLIENTS))}.")
    return ManagedProcessPaths(
        pid_path=config_dir / "run" / "clients" / f"{client}.pid",
        log_path=config_dir / "logs" / "clients" / f"{client}.log",
    )


def normalize_client_values(
    values: Optional[Sequence[str]],
    *,
    default: str,
) -> list[str]:
    """Normalize comma-separated client values."""
    raw_values: Sequence[str] = values if values else (default,)
    selected: list[str] = []
    for raw_value in raw_values:
        for token in str(raw_value).split(","):
            client = token.strip().lower()
            if not client:
                continue
            if client not in VALID_CLIENTS:
                valid = ", ".join(sorted(VALID_CLIENTS))
                raise ClientSelectionError(f"Unknown client {client!r}. Expected one of: {valid}.")
            if client not in selected:
                selected.append(client)
    if not selected:
        raise ClientSelectionError("No client selected.")
    return selected


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
    "CLIENT_WEB",
    "VALID_CLIENTS",
    "ClientSelectionError",
    "client_paths",
    "load_config_file",
    "normalize_client_values",
    "web_client_config",
    "web_client_public_url",
    "api_url_to_ws_url",
]
