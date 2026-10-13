"""Agent-local online device sessions and correlated execution results."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from pathlib import Path
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from .config import DeviceConfig, DeviceId, Name
from .credentials import DeviceCredentials


logger = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 64 * 1024
MAX_PENDING_COMMANDS = 256
PAIRING_SECONDS = 300


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    description: str = Field(default="", max_length=2048)
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["register"]
    device_id: DeviceId
    name: str = Field(default="", max_length=256)
    location: str = Field(default="", max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[Capability] = Field(min_length=1, max_length=64)


class CallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    device_id: DeviceId
    capability: Name
    params: dict[str, Any] = Field(default_factory=dict)


class DeviceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["result"]
    command_id: str = Field(min_length=1, max_length=128)
    ok: StrictBool
    data: Any = None
    error: str = Field(default="", max_length=4096)


class DeviceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["event"]
    event_id: str = Field(min_length=1, max_length=128)
    event_type: Name
    context: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_BYTES)]
    data: dict[str, Any] | None = None


@dataclass(eq=False)
class Connection:
    websocket: Any
    registration: Registration
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass
class PendingCommand:
    connection: Connection
    future: asyncio.Future
    sent: bool = False


def outcome(status: str, code: str, message: str, **details) -> dict:
    return {"status": status, "code": code, "message": message, **details}


def _invalid_constant(value: str):
    raise ValueError(f"Invalid JSON number: {value}")


def load_message(raw: str | bytes):
    size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
    if size > MAX_MESSAGE_BYTES:
        raise ValueError("Message exceeds 64 KiB")
    return json.loads(raw, parse_constant=_invalid_constant)


class DeviceService:
    def __init__(self, config: DeviceConfig, credentials_path: Path | None = None):
        self.config = config
        self.credentials = DeviceCredentials(credentials_path)
        self._pairing_code = ""
        self._pairing_deadline = 0.0
        self.devices: dict[str, Connection] = {}
        self.pending: dict[str, PendingCommand] = {}
        self.accepting = False
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def resources_closed(self) -> bool:
        return not self.devices and not self.pending

    def start(self) -> None:
        if not self.resources_closed:
            raise RuntimeError("Previous device sessions have not finished cleanup")
        self.accepting = True

    def quiesce(self) -> None:
        self.accepting = False
        self.cancel_pairing()

    def cancel_pairing(self) -> None:
        self._pairing_code = ""
        self._pairing_deadline = 0.0

    def create_pairing(self) -> dict:
        if not self.accepting:
            raise RuntimeError("Connect the Agent API before adding a device")
        self._pairing_code = "pair_" + secrets.token_urlsafe(18)
        self._pairing_deadline = time.monotonic() + PAIRING_SECONDS
        return {
            "pairing_code": self._pairing_code, "expires_in": PAIRING_SECONDS,
            "expires_at": datetime.fromtimestamp(time.time() + PAIRING_SECONDS, timezone.utc).isoformat(),
        }

    def _valid_pairing(self, token: str) -> bool:
        return bool(self._pairing_code) and time.monotonic() < self._pairing_deadline and secrets.compare_digest(
            token.encode("utf-8"), self._pairing_code.encode("utf-8"),
        )

    def complete_pairing(self, registration: Registration, credential: str) -> str | None:
        """Validate the manifest before consuming a code; bind a new ID atomically."""
        if not self.accepting:
            raise RuntimeError("Device service is unavailable")
        self._validate_registration(registration)
        device_id = registration.device_id
        token = credential.removeprefix("Bearer ") if credential.startswith("Bearer ") else ""
        if self.credentials.authenticate(device_id, token):
            return None
        if device_id in self.credentials.digests or not self._valid_pairing(token):
            raise ValueError("Invalid pairing code or device ID is already paired")
        issued = "dvc_" + secrets.token_urlsafe(32)
        self.credentials.add(device_id, issued)
        self.cancel_pairing()
        logger.info("Device paired id=%s", device_id)
        return issued

    async def revoke(self, device_id: str) -> None:
        self.credentials.revoke(device_id)
        connection = self.devices.get(device_id)
        if connection is not None:
            await self.disconnect(connection)
        logger.info("Device removed id=%s", device_id)

    async def stop(self, drain_timeout: float = 0) -> None:
        self.quiesce()
        if drain_timeout > 0:
            try:
                await asyncio.wait_for(self._idle.wait(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                pass
        # Complete pending calls before waiting for transport closure, even
        # when a sender is stalled by backpressure. Close devices concurrently.
        await self.close_sessions()

    async def close_sessions(self) -> None:
        await asyncio.gather(*(self.disconnect(conn) for conn in list(self.devices.values())))
        await self._idle.wait()

    def authenticate(self, device_id: str, credential: str) -> bool:
        if not credential.startswith("Bearer "):
            return False
        token = credential.removeprefix("Bearer ")
        return self.credentials.authenticate(device_id, token) or (
            device_id not in self.credentials.digests and self._valid_pairing(token)
        )

    def list_devices(self, *, device_id: str = "", location: str = "", capability: str = "") -> dict:
        rows = []
        for key, connection in self.devices.items():
            registration = connection.registration
            if device_id and key != device_id:
                continue
            if location and registration.location != location:
                continue
            if capability and capability not in {cap.name for cap in registration.capabilities}:
                continue
            rows.append(registration.model_dump(exclude={"type"}))
        return {"status": "success", "devices": rows}

    async def register(self, websocket, registration: Registration) -> Connection:
        if not self.accepting:
            raise RuntimeError("Device service is unavailable")
        if registration.device_id not in self.credentials.digests:
            raise ValueError("Device is not paired with this Agent")
        self._validate_registration(registration)
        connection = Connection(websocket, registration)
        previous = self.devices.get(registration.device_id)
        self.devices[registration.device_id] = connection
        if previous is not None:
            try:
                await self.disconnect(previous)
            except asyncio.CancelledError:
                await self.disconnect(connection)
                raise
        logger.info("Device registered id=%s", registration.device_id)
        return connection

    @staticmethod
    def _validate_registration(registration: Registration) -> None:
        names = [cap.name for cap in registration.capabilities]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate capability names")
        if any(cap.parameters.get("type", "object") != "object" for cap in registration.capabilities):
            raise ValueError("Capability parameters must describe an object")

    async def disconnect(self, connection: Connection) -> None:
        device_id = connection.registration.device_id
        if self.devices.get(device_id) is connection:
            del self.devices[device_id]
        for command_id, pending in list(self.pending.items()):
            if pending.connection is connection and not pending.future.done():
                pending.future.set_result(outcome(
                    "unknown" if pending.sent else "error", "device_disconnected",
                    "Device disconnected; execution may have occurred" if pending.sent else "Device disconnected before sending",
                    command_id=command_id, device_id=device_id,
                ))
        try:
            await asyncio.wait_for(connection.websocket.close(code=1001), timeout=1)
        except Exception:
            pass  # The transport may already have disconnected.

    def resolve(self, connection: Connection, result: DeviceResult) -> None:
        pending = self.pending.get(result.command_id)
        if (
            pending is None or pending.connection is not connection or not pending.sent
            or self.devices.get(connection.registration.device_id) is not connection
            or pending.future.done()
        ):
            return
        response = {
            "status": "success" if result.ok else "error", "command_id": result.command_id,
            "device_id": connection.registration.device_id, "data": result.data,
        }
        if not result.ok:
            response.update(code="device_error", message=result.error or "Device execution failed")
        pending.future.set_result(response)

    async def call(self, request: CallRequest) -> dict:
        connection = self.devices.get(request.device_id)
        if not self.accepting or connection is None:
            return outcome("error", "device_unavailable", "Device API is unavailable or device is offline", device_id=request.device_id)
        if request.capability not in {cap.name for cap in connection.registration.capabilities}:
            return outcome("error", "capability_unavailable", "Device does not declare this capability", device_id=request.device_id)
        if len(self.pending) >= MAX_PENDING_COMMANDS:
            return outcome("error", "busy", "Device command capacity reached")
        command_id = uuid.uuid4().hex
        command = {"type": "command", "command_id": command_id, "capability": request.capability, "params": request.params}
        try:
            load_message(json.dumps(command, ensure_ascii=False, separators=(",", ":"), allow_nan=False))
        except (ValueError, TypeError):
            return outcome("error", "invalid_arguments", "Command must be JSON within 64 KiB")
        pending = PendingCommand(connection, asyncio.get_running_loop().create_future())
        self.pending[command_id] = pending
        self._idle.clear()

        async def execute():
            try:
                async with connection.lock:
                    if pending.future.done():
                        return
                    if self.devices.get(request.device_id) is not connection:
                        pending.future.set_result(outcome("error", "device_disconnected", "Device connection changed before sending", command_id=command_id, device_id=request.device_id))
                        return
                    pending.sent = True  # A partial send is an uncertain side effect.
                    await connection.websocket.send_json(command)
                    await asyncio.shield(pending.future)
            except Exception:
                if not pending.future.done():
                    pending.future.set_result(outcome("unknown" if pending.sent else "error", "device_disconnected", "Device command delivery unavailable; do not automatically retry", command_id=command_id, device_id=request.device_id))

        execution = asyncio.create_task(execute())
        try:
            response = await asyncio.wait_for(asyncio.shield(pending.future), timeout=self.config.timeout_seconds)
        except asyncio.TimeoutError:
            response = outcome(
                "unknown" if pending.sent else "error", "timeout" if pending.sent else "busy",
                "No definitive result; do not automatically retry" if pending.sent else "Device busy; command was not sent",
                command_id=command_id, device_id=request.device_id,
            )
        finally:
            execution.cancel()
            try:
                await asyncio.gather(execution, return_exceptions=True)
            finally:
                pending.future.cancel()
                self.pending.pop(command_id, None)
                if not self.pending:
                    self._idle.set()
        logger.info("Device command id=%s device=%s status=%s", command_id, request.device_id, response["status"])
        return response
