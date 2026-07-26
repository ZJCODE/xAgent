"""Foreground command for the loopback-only browser management UI."""
from __future__ import annotations

import argparse
from pathlib import Path

from ..web import WebClientServer
from ..web.server import normalize_loopback_host
from .agents import AgentRegistryError, resolve_agent_runtime_dir


DEFAULT_WEB_HOST = "127.0.0.1"
DEFAULT_WEB_PORT = 1415


def loopback_host(value: str) -> str:
    try:
        return normalize_loopback_host(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def web_port(value: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def web_url(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def handle_web(args: argparse.Namespace) -> int:
    agent_name = getattr(args, "agent", None)
    config_dir: Path | None = None
    if agent_name:
        try:
            config_dir = resolve_agent_runtime_dir(agent_name)
        except AgentRegistryError as exc:
            print(f"Web UI failed: {exc}")
            return 1

    try:
        server = WebClientServer(
            host=args.host,
            port=args.port,
            config_dir=str(config_dir) if config_dir is not None else None,
            initial_agent=agent_name,
        )
    except (OSError, ValueError) as exc:
        print(f"Web UI failed: {exc}")
        return 1

    url = web_url(server.host, server.port)
    print(f"xAgent Web UI: {url}")
    print("Press Ctrl+C to stop the Web UI. Agent Runtime processes are unaffected.")
    try:
        server.run(open_browser=bool(args.open_browser))
    except KeyboardInterrupt:
        return 0
    return 0
