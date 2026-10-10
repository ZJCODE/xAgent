"""Runtime channel management routes for the built-in web client."""

from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ..base import BaseAgentConfig
from ..cli.channels import (
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
from ..cli.processes import (
    managed_paths,
    running_pid,
    start_background,
    stop_managed_process,
    tail_text,
)
from ..voice.config import VoiceChannelConfig
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
        from ..cli.agent_runtime import runtime_status
        runtime = await runtime_status(config_dir)
        rows = [_channel_status(config_dir, config, channel) for channel in MANAGED_CHANNELS]
        states = runtime.get("channels", {})
        for row in rows:
            state = states.get(row["id"], {})
            if isinstance(state, dict) and state:
                raw_status = state.get("status", "stopped")
                row["status"] = {"connected": "running", "failed": "error"}.get(raw_status, raw_status)
                row["detail"] = state.get("error") or row["detail"]
            row.update(can_start=False, can_stop=False, can_restart=False)
        return {"config_dir": str(config_dir), "channels": rows, "runtime": runtime}

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

    @app.get("/api/runtime", tags=["Runtime"])
    async def get_runtime():
        from ..cli.agent_runtime import runtime_status
        return await runtime_status(resolve_config_dir())

    @app.post("/api/runtime/start", tags=["Runtime"])
    async def start_agent():
        from ..cli.agent_runtime import start_runtime
        try:
            return await asyncio.to_thread(start_runtime, resolve_config_dir())
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/runtime/stop", tags=["Runtime"])
    async def stop_agent():
        from ..cli.agent_runtime import stop_runtime
        try:
            return await stop_runtime(resolve_config_dir())
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.post("/api/runtime/restart", tags=["Runtime"])
    async def restart_agent():
        await stop_agent()
        return await start_agent()

    @app.get("/api/runtime/logs", tags=["Runtime"])
    async def runtime_logs(lines: int = Query(80, ge=1, le=1000)):
        from ...core.runtime.ownership import runtime_paths
        path = runtime_paths(resolve_config_dir()).log_path
        return {"log_path": str(path), "text": tail_text(path, max_lines=lines), "lines": lines}

    @app.get("/api/channels/{channel}/logs", tags=["Channels"])
    async def channel_logs(channel: str, lines: int = Query(80, ge=1, le=1000)):
        _normalize_channel(channel)
        return {"channel": channel, **await runtime_logs(lines)}


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
    paths = managed_paths(config_dir, "runtime")
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
        configured = bool(data)
        if not configured:
            return False, False, "", ""
        try:
            voice = VoiceChannelConfig.from_dict(data)
            voice.resolved_api_key()
        except ValueError as exc:
            return True, False, str(exc), ""
        return True, True, "soniox half-duplex", ""

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
