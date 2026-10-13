"""Execution-device sockets hosted by the Agent's existing public API."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Awaitable, Callable

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from starlette.requests import ClientDisconnect

from ...integrations.devices.config import DeviceId
from ...integrations.devices.media import MediaError
from ...integrations.devices.service import DeviceEvent, DeviceResult, DeviceService, Registration, load_message


def register_device_routes(
    app: FastAPI,
    service: DeviceService,
    *,
    on_event: Callable[..., Awaitable[None]],
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
