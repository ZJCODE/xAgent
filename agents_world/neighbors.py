"""Discover nearby xAgent processes for the inhabitant page.

This is page-sidecar convenience, not world physics. The world never starts
minds, never reads diaries, and never writes under ~/.xagent/agents/.
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml

DEFAULT_XAGENT_ROOT = "~/.xagent"


def list_local_agents(*, root: Optional[Path | str] = None) -> list[dict[str, Any]]:
    """Return registered local agents with API URLs and a cheap liveness probe."""
    base = Path(root or DEFAULT_XAGENT_ROOT).expanduser().resolve()
    registry_path = base / "agents.yaml"
    if not registry_path.is_file():
        return []

    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    agents_raw = raw.get("agents")
    if not isinstance(agents_raw, dict):
        return []

    neighbors: list[dict[str, Any]] = []
    for name, entry in agents_raw.items():
        agent_name = str(name or "").strip()
        if not agent_name:
            continue
        title = agent_name
        agent_dir = base / "agents" / agent_name
        if isinstance(entry, dict):
            title = str(entry.get("title") or title).strip() or agent_name
            path_raw = str(entry.get("path") or "").strip()
            if path_raw:
                agent_dir = Path(path_raw).expanduser()
                if not agent_dir.is_absolute():
                    agent_dir = (base / agent_dir).resolve()
        api = _api_from_config(agent_dir / "config.yaml")
        running = _port_open(api["host"], api["port"])
        neighbors.append(
            {
                "name": agent_name,
                "title": title,
                "api_url": api["url"],
                "running": running,
                "world_ready": _world_ready(api["url"]) if running else False,
            }
        )
    neighbors.sort(key=lambda item: str(item["name"]))
    return neighbors


def _api_from_config(config_path: Path) -> dict[str, Any]:
    host = "127.0.0.1"
    port = 8010
    if config_path.is_file():
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            channels = data.get("channels")
            api = channels.get("api") if isinstance(channels, dict) else None
            if isinstance(api, dict):
                host = str(api.get("host") or host).strip() or host
                try:
                    port = int(api.get("port") or port)
                except (TypeError, ValueError):
                    port = 8010
    browse_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return {"host": browse_host, "port": port, "url": f"http://{browse_host}:{port}"}


def _world_ready(api_url: str) -> bool:
    """True when the running API process exposes /world/status (current code)."""
    url = api_url.rstrip("/") + "/world/status"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=0.4) as resp:
            return 200 <= int(resp.status) < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def _port_open(host: str, port: int) -> bool:
    parsed = urlparse(f"http://{host}")
    target = parsed.hostname or host
    try:
        with socket.create_connection((target, int(port)), timeout=0.2):
            return True
    except OSError:
        return False
