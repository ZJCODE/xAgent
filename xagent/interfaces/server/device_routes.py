"""Execution-device sockets hosted by the Agent's existing public API."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from ...integrations.devices.config import DeviceId
from ...integrations.devices.media import MediaError
from ...integrations.devices.service import (
    DeviceEvent,
    DeviceResult,
    DeviceService,
    Registration,
    VoiceDeviceSession,
    load_message,
)
from ..voice.config import SONIOX_TTS_CHANNELS, SONIOX_TTS_SAMPLE_RATE, VoiceChannelConfig
from ..voice.factory import create_websocket_voice_runtime
from ..voice.runtime import VoiceRuntimeOptions
from ..voice.ws import (
    MAX_VOICE_AUDIO_CHUNK_BYTES,
    VoiceClientControl,
    VoiceSessionHello,
    WebSocketVoiceBridge,
    parse_voice_control_frame,
)


def register_device_routes(
    app: FastAPI,
    service: DeviceService,
    *,
    on_event: Callable[..., Awaitable[None]],
    agent: Any = None,
    tasks_dir: Any = None,
    voice_config_provider: Callable[[], dict[str, Any]] | None = None,
) -> None:
    @app.put("/api/device-media/{device_id}/{attachment_id}", tags=["Devices"])
    async def upload_device_media(request: Request, device_id: DeviceId, attachment_id: str,
                                  filename: str = "attachment.bin"):
        headers = {"Cache-Control": "no-store"}
        try:
            credential = request.headers.get("authorization", "")
            if not service.authenticate_media(device_id, credential):
                raise MediaError(401, "device_unauthorized", "A paired device credential is required")
            if request.headers.get("content-encoding", "identity").lower() != "identity":
                raise MediaError(415, "unsupported_encoding", "Compressed request bodies are not supported")
            raw_length = request.headers.get("content-length")
            if raw_length is None:
                raise MediaError(411, "length_required", "Content-Length is required")
            if not re.fullmatch(r"[0-9]{1,20}", raw_length):
                raise MediaError(400, "invalid_length", "Content-Length must be a nonnegative integer")
            metadata, created = await service.upload_media(
                device_id, credential, attachment_id=attachment_id, file_name=filename,
                mime_type=request.headers.get("content-type", ""), size_bytes=int(raw_length),
                sha256=request.headers.get("x-content-sha256"), chunks=request.stream(),
            )
            return JSONResponse(metadata, status_code=201 if created else 200, headers=headers)
        except MediaError as exc:
            if exc.status == 401:
                headers["WWW-Authenticate"] = "Bearer"
            if exc.status == 429:
                headers["Retry-After"] = "1"
            return JSONResponse({"code": exc.code, "detail": str(exc)}, status_code=exc.status, headers=headers)
        except ClientDisconnect:
            return JSONResponse({"code": "upload_disconnected", "detail": "Upload connection was closed"}, status_code=400, headers=headers)
        except (OSError, sqlite3.Error):
            return JSONResponse({"code": "media_storage_error", "detail": "Device media storage is unavailable"}, status_code=500, headers=headers)

    @app.websocket("/ws/devices/{device_id}")
    async def device_socket(websocket: WebSocket, device_id: str):
        credential = websocket.headers.get("authorization", "")
        if not service.authenticate(device_id, credential):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        connection = None
        try:
            if not service.accepting:
                await websocket.close(code=1013, reason="Device API is unavailable")
                return
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            registration = Registration.model_validate(load_message(raw))
            if registration.device_id != device_id:
                raise ValueError("Registration identity does not match credential")
            issued = service.complete_pairing(registration, credential)
            if issued is not None:
                await asyncio.wait_for(websocket.send_json({
                    "type": "paired", "device_id": device_id, "credential": issued,
                }), timeout=5)
            connection = await service.register(websocket, registration)
            while service.devices.get(device_id) is connection:
                message = load_message(await websocket.receive_text())
                # A replacement can register while this socket waits for input.
                if service.devices.get(device_id) is not connection:
                    break
                if isinstance(message, dict) and message.get("type") == "event":
                    event = DeviceEvent.model_validate(message)
                    if service.accepting:
                        attachments = await service.resolve_attachments(connection, event.attachments)
                        if service.accepting and service.devices.get(device_id) is connection:
                            if attachments:
                                await on_event(device_id, event, attachments=attachments)
                            else:
                                await on_event(device_id, event)
                else:
                    await service.resolve_result(connection, DeviceResult.model_validate(message))
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            # Runtime cancels transport handlers after the drain deadline.
            # Complete that planned disconnect without an ASGI error report.
            if service.accepting:
                raise
        except (ValueError, asyncio.TimeoutError, KeyError):
            try:
                await websocket.close(code=1008, reason="Invalid device message or registration")
            except RuntimeError:
                pass
        except RuntimeError:
            try:
                await websocket.close(code=1013, reason="Device API is unavailable")
            except RuntimeError:
                pass
        except (OSError, sqlite3.Error):
            await websocket.close(code=1011, reason="Unable to persist device data")
        finally:
            if connection is not None:
                await service.disconnect(connection)
            else:
                try:
                    await asyncio.wait_for(websocket.close(code=1001), timeout=1)
                except Exception:
                    pass

    @app.websocket("/ws/devices/{device_id}/voice")
    async def device_voice_socket(websocket: WebSocket, device_id: str):
        credential = websocket.headers.get("authorization", "")
        if not service.authenticate_paired(device_id, credential):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        session: VoiceDeviceSession | None = None
        try:
            if not service.accepting or agent is None:
                await websocket.close(code=1013, reason="Device voice API is unavailable")
                return
            raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=5)
            hello = VoiceSessionHello.model_validate(parse_voice_control_frame(raw_hello))
            raw_voice_cfg = voice_config_provider() if callable(voice_config_provider) else {}
            voice_cfg = VoiceChannelConfig.from_dict(raw_voice_cfg)
            voice_cfg.resolved_api_key()
            room_name = service.resolve_room_name(device_id)
            bridge = WebSocketVoiceBridge(
                websocket=websocket,
                loop=asyncio.get_running_loop(),
                hello=hello,
                room_name=room_name,
                send_lock=asyncio.Lock(),
                closed=threading.Event(),
            )
            options = VoiceRuntimeOptions(
                user_id=(hello.user_id or "").strip() or f"device:{device_id}",
                tasks_dir=tasks_dir,
            )
            runtime, microphone, player = create_websocket_voice_runtime(
                agent=agent,
                config=voice_cfg,
                bridge=bridge,
                options=options,
                device_id=device_id,
            )
            session = VoiceDeviceSession(
                device_id=device_id,
                websocket=websocket,
                runtime=runtime,
                microphone=microphone,
                player=player,
                room_name=room_name,
            )
            await service.register_voice(session)
            await asyncio.wait_for(
                websocket.send_json(
                    {
                        "type": "ready",
                        "device_id": device_id,
                        "room_name": room_name,
                        "input_sample_rate": hello.input_sample_rate,
                        "input_channels": hello.input_channels,
                        "output_sample_rate": hello.output_sample_rate or SONIOX_TTS_SAMPLE_RATE,
                        "output_channels": hello.output_channels or SONIOX_TTS_CHANNELS,
                    }
                ),
                timeout=5,
            )
            session.runtime_task = asyncio.create_task(runtime.run_forever())
            while service.voice_sessions.get(device_id) is session and not session.runtime_task.done():
                receive_task = asyncio.create_task(websocket.receive())
                done, _ = await asyncio.wait(
                    {receive_task, session.runtime_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if receive_task not in done:
                    receive_task.cancel()
                    await asyncio.gather(receive_task, return_exceptions=True)
                    break
                frame = receive_task.result()
                if frame.get("type") == "websocket.disconnect":
                    break
                if service.voice_sessions.get(device_id) is not session:
                    break
                raw_bytes = frame.get("bytes")
                if raw_bytes is not None:
                    if len(raw_bytes) > MAX_VOICE_AUDIO_CHUNK_BYTES:
                        raise ValueError("Voice audio chunk exceeds 256 KiB")
                    microphone.push_audio(raw_bytes)
                    continue
                raw_text = frame.get("text")
                if raw_text is not None:
                    control = VoiceClientControl.model_validate(parse_voice_control_frame(raw_text))
                    if control.type == "playback_progress":
                        runtime.note_remote_playback_progress(control.played_bytes)
                    elif control.type == "ping":
                        await websocket.send_json({"type": "pong"})
                    elif control.type == "stop":
                        break
            if session.runtime_task is not None and session.runtime_task.done() and not session.runtime_task.cancelled():
                exc = session.runtime_task.exception()
                if exc is not None:
                    raise exc
        except WebSocketDisconnect:
            pass
        except asyncio.CancelledError:
            if service.accepting:
                raise
        except (ValueError, asyncio.TimeoutError, KeyError) as exc:
            try:
                await websocket.close(code=1008, reason=str(exc)[:120] or "Invalid voice handshake or frame")
            except RuntimeError:
                pass
        except RuntimeError:
            try:
                await websocket.close(code=1013, reason="Device voice API is unavailable")
            except RuntimeError:
                pass
        except Exception:
            try:
                await websocket.close(code=1011, reason="Voice runtime error")
            except RuntimeError:
                pass
        finally:
            if session is not None:
                await service.disconnect_voice(session)
            else:
                try:
                    await asyncio.wait_for(websocket.close(code=1001), timeout=1)
                except Exception:
                    pass
