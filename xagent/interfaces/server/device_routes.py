"""Execution-device sockets hosted by the Agent's existing public API."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ...integrations.devices.service import DeviceEvent, DeviceResult, DeviceService, Registration, load_message


def register_device_routes(
    app: FastAPI,
    service: DeviceService,
    *,
    on_event: Callable[[str, DeviceEvent], Awaitable[None]],
) -> None:
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
                        await on_event(device_id, event)
                else:
                    service.resolve(connection, DeviceResult.model_validate(message))
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
        except OSError:
            await websocket.close(code=1011, reason="Unable to persist device data")
        finally:
            if connection is not None:
                await service.disconnect(connection)
            else:
                try:
                    await asyncio.wait_for(websocket.close(code=1001), timeout=1)
                except Exception:
                    pass
