"""The small Agent-facing voice settings surface; shared by all transports."""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ..cli.config_editor import apply_agent_edit_setup, load_config
from ..voice.config import (
    SONIOX_SHARED_VOICE_OPTIONS,
    SONIOX_TTS_SPEED_MAX,
    SONIOX_TTS_SPEED_MIN,
    VoiceChannelConfig,
)


class VoiceSettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice: str | None = None
    voice_api_key: str = ""
    speed: float | None = Field(
        default=None,
        strict=True,
        ge=SONIOX_TTS_SPEED_MIN,
        le=SONIOX_TTS_SPEED_MAX,
        allow_inf_nan=False,
    )
    interruptions: StrictBool | None = None
    audio_input: str | StrictInt | None = None
    audio_output: str | StrictInt | None = None


def voice_settings_status(config: dict) -> dict:
    channels = config.get("channels")
    raw = channels.get("voice") if isinstance(channels, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    try:
        VoiceChannelConfig.from_dict({"api_key": raw.get("api_key")}).resolved_api_key()
        has_api_key = True
    except ValueError:
        has_api_key = False
    try:
        voice = VoiceChannelConfig.from_dict(raw)
        configured = has_api_key
    except ValueError:
        voice = VoiceChannelConfig.from_dict({})
        configured = False
    return {
        "configured": configured,
        "has_api_key": has_api_key,
        "voice": voice.speaking_voice,
        "speed": voice.speaking_speed,
        "interruptions": voice.interruptions,
        "audio_input": voice.audio.input,
        "audio_output": voice.audio.output,
        "voice_options": [{"id": name, "label": f"{name} · {description}"}
                          for name, description in SONIOX_SHARED_VOICE_OPTIONS],
    }


def register_voice_settings_routes(app: FastAPI, resolve_admin, notify_config_written) -> None:
    @app.get("/api/agent/voice/devices", tags=["Voice"])
    async def agent_voice_devices(response: Response):
        from ..voice.audio import list_audio_device_options
        response.headers["Cache-Control"] = "no-store"
        return await asyncio.to_thread(list_audio_device_options)

    @app.get("/api/agent/voice", tags=["Voice"])
    async def agent_voice_settings(response: Response):
        from ..cli.agent_runtime import runtime_status
        response.headers["Cache-Control"] = "no-store"
        server = resolve_admin()
        settings = voice_settings_status(load_config(Path(server.config_dir)))
        runtime = await runtime_status(Path(server.config_dir))
        return {**settings, "restart_required": bool(runtime.get("needs_restart"))}

    @app.patch("/api/agent/voice", tags=["Voice"])
    async def update_agent_voice(selection: VoiceSettingsInput, response: Response):
        response.headers["Cache-Control"] = "no-store"
        server = resolve_admin()
        try:
            result = apply_agent_edit_setup(Path(server.config_dir), "voice", selection.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        server.config = load_config(Path(server.config_dir))
        notify_config_written()
        # Offline admin writes also briefly hold the ownership lock. Only the
        # actual runtime owner needs a restart, not that temporary writer.
        result["restart_required"] = bool(result["changed"] and getattr(server, "is_runtime_owner", False))
        return result
