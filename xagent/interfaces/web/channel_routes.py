"""Runtime channel management routes for the built-in web client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ...core.agent_factory import AgentPaths
from ...core.runtime import (
    RuntimeClient,
    RuntimeLaunchError,
    RuntimeLauncher,
    RuntimeUnavailable,
)
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
from .qr_sessions import get_qr_session_manager
from .session import WebAgentSession

ChannelId = Literal["api", "voice", "feishu", "weixin"]
SetupChannelId = Literal["voice", "feishu", "weixin"]

CHANNEL_LABELS: dict[str, str] = {
    CHANNEL_API: "Public API",
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
        config = load_config_file(config_dir)
        runtime = await _runtime_snapshot(config_dir)
        return {
            "config_dir": str(config_dir),
            "channels": [
                _channel_status(config_dir, config, channel, runtime)
                for channel in MANAGED_CHANNELS
            ],
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
            config = load_config_file(config_dir)
            return {
                "status": "ok",
                "setup": result,
                "channel": _channel_status(
                    config_dir,
                    config,
                    normalized,
                    await _runtime_snapshot(config_dir),
                ),
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
        config = load_config_file(config_dir)
        status = _channel_status(
            config_dir,
            config,
            channel,
            await _runtime_snapshot(config_dir),
        )
        if not status["ready"]:
            raise HTTPException(
                status_code=400,
                detail=f"{status['label']} is not configured. Set it up from the Channels page.",
            )
        if status["status"] == "running":
            return {"status": "ok", "message": f"{channel} already running", "channel": status}
        try:
            await asyncio.to_thread(RuntimeLauncher(config_dir).start)
        except RuntimeLaunchError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await asyncio.to_thread(
            RuntimeClient(config_dir).request,
            "POST",
            f"/v1/channels/{channel}/start",
        )
        updated = _channel_status(
            config_dir,
            load_config_file(config_dir),
            channel,
            await _runtime_snapshot(config_dir),
        )
        return {"status": "ok", "message": f"started {channel}", "channel": updated}

    @app.post("/api/channels/{channel}/stop", tags=["Channels"])
    async def stop_channel(channel: str):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        try:
            await asyncio.to_thread(
                RuntimeClient(config_dir).request,
                "POST",
                f"/v1/channels/{channel}/stop",
            )
        except RuntimeUnavailable:
            from ...settings import XAgentSettings

            settings = XAgentSettings.load(config_dir / "config.yaml")
            settings.with_channel_enabled(channel, False).write_atomic(
                config_dir / "config.yaml"
            )
        updated = _channel_status(
            config_dir,
            load_config_file(config_dir),
            channel,
            await _runtime_snapshot(config_dir),
        )
        return {"status": "ok", "message": f"stopped {channel}", "channel": updated}

    @app.post("/api/channels/{channel}/restart", tags=["Channels"])
    async def restart_channel(channel: str):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        config = load_config_file(config_dir)
        status = _channel_status(config_dir, config, channel)
        if not status["ready"]:
            raise HTTPException(
                status_code=400,
                detail=f"{status['label']} is not configured. Set it up from the Channels page.",
            )

        try:
            await asyncio.to_thread(RuntimeLauncher(config_dir).start)
        except RuntimeLaunchError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        await asyncio.to_thread(
            RuntimeClient(config_dir).request,
            "POST",
            f"/v1/channels/{channel}/restart",
        )
        updated = _channel_status(
            config_dir,
            load_config_file(config_dir),
            channel,
            await _runtime_snapshot(config_dir),
        )
        return {"status": "ok", "message": f"restarted {channel}", "channel": updated}

    @app.get("/api/channels/{channel}/logs", tags=["Channels"])
    async def channel_logs(channel: str, lines: int = Query(80, ge=1, le=500)):
        channel = _normalize_channel(channel)
        config_dir = resolve_config_dir().expanduser().resolve()
        log_path = config_dir / "run" / "runtime.log"
        text = await asyncio.to_thread(_tail_text, log_path, lines)
        return {
            "channel": channel,
            "log_path": str(log_path),
            "text": text,
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


def _channel_status(
    config_dir: Path,
    config: dict[str, Any],
    channel: str,
    runtime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = next(
        (item for item in (runtime or {}).get("channels", []) if item.get("name") == channel),
        None,
    )
    pid = int(runtime["pid"]) if runtime and runtime.get("pid") else None
    configured, ready, detail, _setup_hint = _readiness(config, channel)
    runtime_status = str(row.get("state") if row else "runtime-stopped")
    enabled = bool(row.get("enabled")) if row else bool(
        ((config.get("channels") or {}).get(channel) or {}).get("enabled", False)
    )
    if not ready:
        runtime_status = "disabled" if not configured else "error"
    if row and row.get("error"):
        detail = str(row["error"])
    elif pid is not None and ready and row:
        detail = f"{detail} pid {pid}".strip()

    return {
        "id": channel,
        "label": CHANNEL_LABELS[channel],
        "status": runtime_status,
        "configured": configured,
        "ready": ready,
        "enabled": enabled,
        "pid": pid,
        "detail": detail,
        "pid_path": "",
        "log_path": str(config_dir / "run" / "runtime.log"),
        "can_start": ready and runtime_status != "running",
        "can_stop": enabled,
        "can_restart": ready,
        "setup_hint": "",
    }


async def _runtime_snapshot(config_dir: Path) -> dict[str, Any] | None:
    try:
        return await asyncio.to_thread(RuntimeClient(config_dir).status)
    except RuntimeUnavailable:
        return None


def _tail_text(path: Path, lines: int) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(content.splitlines()[-lines:])


def _readiness(config: dict[str, Any], channel: str) -> tuple[bool, bool, str, str]:
    if channel == CHANNEL_API:
        data = api_config(config)
        detail = _api_target(data)
        return True, True, detail, ""

    if channel == CHANNEL_VOICE:
        data = voice_config(config)
        configured = bool(
            data.get("provider")
            or data.get("stt")
            or data.get("tts")
        )
        provider = str(data.get("provider") or "").strip()
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
    host = str(data.get("host") or AgentPaths.DEFAULT_HOST).strip() or AgentPaths.DEFAULT_HOST
    port = str(data.get("port") or AgentPaths.DEFAULT_PORT).strip() or str(AgentPaths.DEFAULT_PORT)
    browse_host = "127.0.0.1" if host == "0.0.0.0" else host
    if ":" in browse_host and not browse_host.startswith("["):
        browse_host = f"[{browse_host}]"
    return f"http://{browse_host}:{port}"
