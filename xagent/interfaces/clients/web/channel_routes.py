"""Runtime channel management routes for the built-in web client."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ...base import BaseAgentConfig
from ...cli.channels import (
    CHANNEL_API,
    CHANNEL_FEISHU,
    CHANNEL_VOICE,
    CHANNEL_WEIXIN,
    api_config,
    feishu_config,
    load_config_file,
    voice_config,
    weixin_config,
)
from ...cli.processes import managed_paths, running_pid, start_background, stop_managed_process, tail_text
from .qr_sessions import get_qr_session_manager
from .session import WebAgentSession

ChannelId = Literal["api", "voice", "feishu", "weixin"]
SetupChannelId = Literal["voice", "feishu", "weixin"]

CHANNEL_LABELS: dict[str, str] = {
    CHANNEL_API: "API",
    CHANNEL_VOICE: "Voice",
    CHANNEL_FEISHU: "Feishu",
    CHANNEL_WEIXIN: "Weixin",
}
MANAGED_CHANNELS: tuple[str, ...] = (CHANNEL_API, CHANNEL_VOICE, CHANNEL_FEISHU, CHANNEL_WEIXIN)
SETUP_CHANNELS: tuple[str, ...] = (CHANNEL_VOICE, CHANNEL_FEISHU, CHANNEL_WEIXIN)

class ChannelSetupInput(BaseModel):
    force: bool = False
    selection: dict[str, Any] = Field(default_factory=dict)


def register_channel_routes(
    app: FastAPI,
    session_or_resolver: WebAgentSession | Callable[[], Path],
) -> None:
    if isinstance(session_or_resolver, WebAgentSession):
        session = session_or_resolver

        def resolve_config_dir() -> Path:
            return session.get_current_config_dir()
    else:
        session = None
        resolve_config_dir = session_or_resolver

    @app.get("/api/channels", tags=["Channels"])
    async def list_channels():
        config_dir = resolve_config_dir().expanduser().resolve()
        config = _safe_load_config(config_dir)
        return {
            "config_dir": str(config_dir),
            "channels": [_channel_status(config_dir, config, channel) for channel in MANAGED_CHANNELS],
        }

    if session is not None:
        @app.get("/api/channels/{channel}/setup-schema", tags=["Channels"])
        async def channel_setup_schema(channel: str):
            return session.channel_setup_schema(channel)

        @app.post("/api/channels/{channel}/setup", tags=["Channels"])
        async def channel_setup(channel: str, input_data: ChannelSetupInput):
            normalized = _normalize_setup_channel(channel)
            result = session.apply_channel_setup(
                normalized,
                selection_data=input_data.selection,
                force=input_data.force,
            )
            config_dir = resolve_config_dir().expanduser().resolve()
            config = _safe_load_config(config_dir)
            return {
                "status": "ok",
                "setup": result,
                "channel": _channel_status(config_dir, config, normalized),
            }

        @app.post("/api/channels/{channel}/qr/start", tags=["Channels"])
        async def start_channel_qr(channel: str):
            normalized = _normalize_setup_channel(channel)
            if normalized not in {CHANNEL_FEISHU, CHANNEL_WEIXIN}:
                raise HTTPException(status_code=400, detail=f"{normalized} does not use QR setup")
            manager = get_qr_session_manager()
            if normalized == CHANNEL_FEISHU:
                qr_session = manager.start_feishu()
            else:
                config_dir = resolve_config_dir().expanduser().resolve()
                qr_session = manager.start_weixin(config_dir=config_dir)
            return qr_session.to_dict()

        @app.get("/api/channels/{channel}/qr/{session_id}", tags=["Channels"])
        async def poll_channel_qr(channel: str, session_id: str):
            normalized = _normalize_setup_channel(channel)
            manager = get_qr_session_manager()
            qr_session = manager.get(session_id)
            if qr_session is None or qr_session.channel != normalized:
                raise HTTPException(status_code=404, detail="QR session not found")
            return qr_session.to_dict()

        @app.delete("/api/channels/{channel}/qr/{session_id}", tags=["Channels"])
        async def cancel_channel_qr(channel: str, session_id: str):
            normalized = _normalize_setup_channel(channel)
            manager = get_qr_session_manager()
            qr_session = manager.get(session_id)
            if qr_session is None or qr_session.channel != normalized:
                raise HTTPException(status_code=404, detail="QR session not found")
            manager.cancel(session_id)
            return {"status": "ok", "session_id": session_id}

    @app.post("/api/channels/{channel}/start", tags=["Channels"])
    async def start_channel(channel: str):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        config = _safe_load_config(config_dir)
        status = _channel_status(config_dir, config, channel)
        if not status["ready"]:
            raise HTTPException(
                status_code=400,
                detail=f"{status['label']} is not configured. Set it up from the Channels page.",
            )
        if status["pid"] is not None:
            return {"status": "ok", "message": f"{channel} already running", "channel": status}

        paths = managed_paths(config_dir, channel)
        result = start_background(
            _channel_command(channel, config_dir),
            pid_path=paths.pid_path,
            log_path=paths.log_path,
        )
        if not result.ok:
            detail = result.error or f"Failed to start {channel}"
            if result.recent_output:
                detail = f"{detail}\n{result.recent_output}"
            raise HTTPException(status_code=500, detail=detail)

        updated = _channel_status(config_dir, _safe_load_config(config_dir), channel)
        return {"status": "ok", "message": f"started {channel}", "channel": updated}

    @app.post("/api/channels/{channel}/stop", tags=["Channels"])
    async def stop_channel(channel: str):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        paths = managed_paths(config_dir, channel)
        stopped, message = stop_managed_process(paths.pid_path)
        if not stopped:
            raise HTTPException(status_code=500, detail=message)
        updated = _channel_status(config_dir, _safe_load_config(config_dir), channel)
        return {"status": "ok", "message": message, "channel": updated}

    @app.post("/api/channels/{channel}/restart", tags=["Channels"])
    async def restart_channel(channel: str):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        config = _safe_load_config(config_dir)
        status = _channel_status(config_dir, config, channel)
        if not status["ready"]:
            raise HTTPException(
                status_code=400,
                detail=f"{status['label']} is not configured. Set it up from the Channels page.",
            )

        paths = managed_paths(config_dir, channel)
        stopped, message = stop_managed_process(paths.pid_path)
        if not stopped:
            raise HTTPException(status_code=500, detail=message)

        result = start_background(
            _channel_command(channel, config_dir),
            pid_path=paths.pid_path,
            log_path=paths.log_path,
        )
        if not result.ok:
            detail = result.error or f"Failed to restart {channel}"
            if result.recent_output:
                detail = f"{detail}\n{result.recent_output}"
            raise HTTPException(status_code=500, detail=detail)

        updated = _channel_status(config_dir, _safe_load_config(config_dir), channel)
        return {"status": "ok", "message": f"restarted {channel}", "channel": updated}

    @app.get("/api/channels/{channel}/logs", tags=["Channels"])
    async def channel_logs(channel: str, lines: int = Query(80, ge=1, le=500)):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        paths = managed_paths(config_dir, channel)
        return {
            "channel": channel,
            "log_path": str(paths.log_path),
            "text": tail_text(paths.log_path, max_lines=lines),
            "lines": lines,
        }


def _normalize_channel(channel: str) -> str:
    normalized = str(channel or "").strip().lower()
    if normalized not in MANAGED_CHANNELS:
        raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
    return normalized


def _normalize_setup_channel(channel: str) -> str:
    normalized = str(channel or "").strip().lower()
    if normalized not in SETUP_CHANNELS:
        raise HTTPException(status_code=404, detail=f"Unknown channel: {channel}")
    return normalized


def _safe_load_config(config_dir: Path) -> dict[str, Any]:
    try:
        return load_config_file(config_dir)
    except Exception:
        return {}


def _channel_command(channel: str, config_dir: Path) -> list[str]:
    return [
        sys.executable,
        "-m",
        "xagent.interfaces.cli",
        "_run-channel",
        channel,
        "--config-dir",
        str(config_dir),
    ]


def _channel_status(config_dir: Path, config: dict[str, Any], channel: str) -> dict[str, Any]:
    paths = managed_paths(config_dir, channel)
    pid = running_pid(paths.pid_path)
    configured, ready, detail, _setup_hint = _readiness(config, channel)
    runtime_status = "running" if pid is not None else "stopped"
    if not ready:
        runtime_status = "disabled" if not configured else "error"
    if pid is not None and ready:
        detail = f"{detail} pid {pid}".strip()

    return {
        "id": channel,
        "label": CHANNEL_LABELS[channel],
        "status": runtime_status,
        "configured": configured,
        "ready": ready,
        "pid": pid,
        "detail": detail,
        "pid_path": str(paths.pid_path),
        "log_path": str(paths.log_path),
        "can_start": ready and pid is None,
        "can_stop": pid is not None,
        "can_restart": ready,
        "setup_hint": "",
    }


def _readiness(config: dict[str, Any], channel: str) -> tuple[bool, bool, str, str]:
    if channel == CHANNEL_API:
        data = api_config(config)
        enabled = data.get("enabled", True) is not False
        detail = _api_target(data)
        return enabled, enabled, detail, "" if enabled else "channels.api.enabled is false"

    if channel == CHANNEL_VOICE:
        data = voice_config(config)
        configured = bool(data) and data.get("enabled") is not False
        provider = str(data.get("provider") or "custom").strip() if isinstance(data, dict) and data else ""
        return configured, configured, provider, ""

    if channel == CHANNEL_FEISHU:
        data = feishu_config(config)
        configured = bool(data.get("app_id") and data.get("app_secret"))
        detail = f"app {data.get('app_id')}" if data.get("app_id") else ""
        return configured, configured, detail, ""

    if channel == CHANNEL_WEIXIN:
        data = weixin_config(config)
        configured = bool(data.get("account_id"))
        detail = f"account {data.get('account_id')}" if data.get("account_id") else ""
        return configured, configured, detail, ""

    return False, False, "", ""


def _api_target(data: dict[str, Any]) -> str:
    host = str(data.get("host") or BaseAgentConfig.DEFAULT_HOST).strip() or BaseAgentConfig.DEFAULT_HOST
    port = str(data.get("port") or BaseAgentConfig.DEFAULT_PORT).strip() or str(BaseAgentConfig.DEFAULT_PORT)
    browse_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"
