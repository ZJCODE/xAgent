"""The small Agent-facing voice settings surface; shared by all transports."""
from __future__ import annotations

import asyncio
import io
import threading
import wave
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from ..cli.config_editor import apply_agent_edit_setup, load_config
from ..voice.config import (
    SONIOX_SHARED_VOICE_OPTIONS,
    SONIOX_TTS_SPEED_MAX,
    SONIOX_TTS_SPEED_MIN,
    VoiceChannelConfig,
    VoiceSpeechStyle,
    SONIOX_TTS_SAMPLE_RATE,
)

VOICE_PREVIEW_TEXT = "你好，很高兴认识你。Hello, it's nice to meet you."
VOICE_PREVIEW_TIMEOUT_SECONDS = 20
VOICE_PREVIEW_MAX_BYTES = SONIOX_TTS_SAMPLE_RATE * 2 * 15


class VoicePreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    voice: str
    speed: float = Field(strict=True, ge=SONIOX_TTS_SPEED_MIN, le=SONIOX_TTS_SPEED_MAX, allow_inf_nan=False)
    voice_api_key: str = ""


def _render_voice_preview(synthesizer, stop: threading.Event) -> bytes:
    pcm = bytearray()
    for chunk in synthesizer.synthesize_chunks(iter([VOICE_PREVIEW_TEXT]), language="zh", stop_event=stop):
        if stop.is_set():
            return b""
        if len(pcm) + len(chunk) > VOICE_PREVIEW_MAX_BYTES:
            raise ValueError("Voice preview exceeded its duration limit")
        pcm.extend(chunk)
    if not pcm or len(pcm) % 2:
        raise ValueError("Voice preview returned invalid audio")
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SONIOX_TTS_SAMPLE_RATE)
        wav.writeframes(pcm)
    return output.getvalue()


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
        "voice_options": [{"id": name, "label": f"{name} · {description}"}
                          for name, description in SONIOX_SHARED_VOICE_OPTIONS],
    }


def register_voice_settings_routes(app: FastAPI, resolve_admin, notify_config_written) -> None:
    @app.post("/api/agent/voice/preview", tags=["Voice"])
    async def preview_agent_voice(selection: VoicePreviewInput, request: Request):
        from ..voice.soniox import SonioxRealtimeTTS

        server = resolve_admin()
        channels = load_config(Path(server.config_dir)).get("channels")
        saved = channels.get("voice", {}) if isinstance(channels, dict) else {}
        saved = saved if isinstance(saved, dict) else {}
        try:
            config = VoiceChannelConfig.from_dict({
                "api_key": selection.voice_api_key.strip() or saved.get("api_key"),
                "voice": selection.voice, "speed": selection.speed,
            })
            key = config.resolved_api_key()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        config.apply_speech_style(VoiceSpeechStyle(voice=config.speaking_voice, speed=config.speaking_speed))
        stop = threading.Event()
        synthesizer = None
        task = None
        try:
            synthesizer = SonioxRealtimeTTS(api_key=key, config=config)
            task = asyncio.create_task(asyncio.to_thread(_render_voice_preview, synthesizer, stop))
            deadline = asyncio.get_running_loop().time() + VOICE_PREVIEW_TIMEOUT_SECONDS
            while not task.done():
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Voice preview timed out")
                if await request.is_disconnected():
                    raise HTTPException(499, "Voice preview cancelled")
                await asyncio.wait({task}, timeout=0.1)
            audio = task.result()
            return Response(audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})
        except TimeoutError as exc:
            raise HTTPException(504, "Voice preview timed out. Please try again.") from exc
        except HTTPException:
            raise
        except Exception as exc:
            # Provider errors can contain credentials; keep the public error generic.
            raise HTTPException(502, "Unable to generate the voice preview. Check your key and try again.") from exc
        finally:
            stop.set()
            if synthesizer is not None:
                await asyncio.to_thread(synthesizer.cancel)
                await asyncio.to_thread(synthesizer.close)
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

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
