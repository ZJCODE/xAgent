"""Agent-local online device sessions and correlated execution results."""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal
from pathlib import Path
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from .config import CommandTimeout, DeviceConfig, DeviceId, Name
from .credentials import DeviceCredentials
from .profiles import DeviceProfiles
from .media import DeviceMediaStore, MediaError, attachment_uuid


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
    timeout_seconds: CommandTimeout | None = None


class AttachmentReference(BaseModel):
    model_config = ConfigDict(extra="forbid")
    attachment_id: uuid.UUID


class DeviceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["result"]
    command_id: str = Field(min_length=1, max_length=128)
    ok: StrictBool
    data: Any = None
    error: str = Field(default="", max_length=4096)
    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=128)


class DeviceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    type: Literal["event"]
    event_id: str = Field(min_length=1, max_length=128)
    event_type: Name
    context: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_BYTES)]
    data: dict[str, Any] | None = None
    attachments: list[AttachmentReference] = Field(default_factory=list, max_length=128)


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


@dataclass(eq=False)
class VoiceDeviceSession:
    device_id: str
    websocket: Any
    runtime: Any
    microphone: Any
    player: Any
    runtime_task: asyncio.Task | None = None
    room_name: str = "local_room"
    closed: bool = False


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
    def __init__(self, config: DeviceConfig, credentials_path: Path | None = None, *, workspace: Path | None = None):
        self.config = config
        self.credentials = DeviceCredentials(credentials_path)
        self.profiles = DeviceProfiles(credentials_path.with_name(".device_profiles.json") if credentials_path else None)
        self.media = DeviceMediaStore(workspace, credentials_path.parent, config.media) if workspace is not None and credentials_path is not None else None
        self.uploads: dict[asyncio.Task, str] = {}
        self._media_prepare: asyncio.Task | None = None
        self._pairing_code = ""
        self._pairing_deadline = 0.0
        self.devices: dict[str, Connection] = {}
        self.voice_sessions: dict[str, VoiceDeviceSession] = {}
        self.pending: dict[str, PendingCommand] = {}
        self.accepting = False
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def resources_closed(self) -> bool:
        return (
            not self.devices
            and not self.voice_sessions
            and not self.pending
            and not self.uploads
            and (self._media_prepare is None or self._media_prepare.done())
        )

    def _update_idle(self) -> None:
        if self.pending or self.uploads:
            self._idle.clear()
        else:
            self._idle.set()

    def start(self) -> None:
        if not self.resources_closed:
            raise RuntimeError("Previous device sessions have not finished cleanup")
        self.accepting = True
        if self.media is not None:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return  # Synchronous embedding prepares lazily on first use.
            self._media_prepare = asyncio.create_task(self.media.recover())
            self._media_prepare.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

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
        uploads = [task for task, owner in self.uploads.items() if owner == device_id]
        for task in uploads:
            task.cancel()
        await asyncio.gather(*uploads, return_exceptions=True)
        connection = self.devices.get(device_id)
        if connection is not None:
            await self.disconnect(connection)
        voice_session = self.voice_sessions.get(device_id)
        if voice_session is not None:
            await self.disconnect_voice(voice_session)
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
        uploads = list(self.uploads)
        for task in uploads:
            task.cancel()
        await asyncio.gather(*uploads, return_exceptions=True)
        if self._media_prepare is not None:
            await asyncio.gather(self._media_prepare, return_exceptions=True)
        await asyncio.gather(
            *(self.disconnect(conn) for conn in list(self.devices.values())),
            *(self.disconnect_voice(sess) for sess in list(self.voice_sessions.values())),
        )
        await self._idle.wait()

    def authenticate_paired(self, device_id: str, credential: str) -> bool:
        """Authenticate an already-paired device credential (rejects one-time pairing codes)."""
        return credential.startswith("Bearer ") and self.credentials.authenticate(device_id, credential[7:])

    def resolve_room_name(self, device_id: str) -> str:
        """Resolve a human-meaningful room/location name for a paired device."""
        fields = self.profile_fields(device_id)
        for candidate in (fields.get("location"), fields.get("name")):
            cleaned = str(candidate or "").strip()
            if cleaned:
                return cleaned
        connection = self.devices.get(device_id)
        if connection is not None:
            for candidate in (connection.registration.location, connection.registration.name):
                cleaned = str(candidate or "").strip()
                if cleaned:
                    return cleaned
        return device_id or "local_room"

    async def register_voice(self, session: VoiceDeviceSession) -> VoiceDeviceSession:
        if not self.accepting:
            raise RuntimeError("Device service is unavailable")
        if session.device_id not in self.credentials.digests:
            raise ValueError("Device is not paired with this Agent")
        previous = self.voice_sessions.get(session.device_id)
        self.voice_sessions[session.device_id] = session
        if previous is not None and previous is not session:
            try:
                await self.disconnect_voice(previous)
            except asyncio.CancelledError:
                await self.disconnect_voice(session)
                raise
        logger.info("Device voice session registered id=%s room=%s", session.device_id, session.room_name)
        return session

    async def disconnect_voice(self, session: VoiceDeviceSession) -> None:
        if session.closed:
            return
        session.closed = True
        if self.voice_sessions.get(session.device_id) is session:
            del self.voice_sessions[session.device_id]
        try:
            session.microphone.close()
        except Exception:
            pass
        try:
            session.player.close()
        except Exception:
            pass
        stop = getattr(session.runtime, "stop", None)
        if callable(stop):
            try:
                await asyncio.wait_for(stop(), timeout=2.0)
            except Exception:
                pass
        if session.runtime_task is not None and not session.runtime_task.done():
            session.runtime_task.cancel()
            await asyncio.gather(session.runtime_task, return_exceptions=True)
        try:
            await asyncio.wait_for(session.websocket.close(code=1001), timeout=1.0)
        except Exception:
            pass

    def authenticate_media(self, device_id: str, credential: str) -> bool:
        return credential.startswith("Bearer ") and self.credentials.authenticate(device_id, credential[7:])

    async def upload_media(self, device_id: str, credential: str, **kwargs) -> tuple[dict, bool]:
        if not self.authenticate_media(device_id, credential):
            raise MediaError(401, "device_unauthorized", "A paired device credential is required")
        if not self.accepting or self.media is None:
            raise MediaError(503, "device_unavailable", "Device media API is unavailable")
        attachment_uuid(kwargs["attachment_id"])
        if len(self.uploads) >= self.config.media.max_uploads or sum(owner == device_id for owner in self.uploads.values()) >= self.config.media.max_device_uploads:
            raise MediaError(429, "upload_busy", "Device upload concurrency limit reached")
        owner = self.credentials.digests[device_id]

        async def execute():
            try:
                if self._media_prepare is not None:
                    await asyncio.shield(self._media_prepare)
                return await self.media.upload(
                    device_id=device_id, owner=owner,
                    authorized=lambda: self.authenticate_media(device_id, credential), **kwargs,
                )
            finally:
                self.uploads.pop(asyncio.current_task(), None)
                self._update_idle()

        task = asyncio.create_task(execute())
        self.uploads[task] = device_id
        self._update_idle()
        try:
            metadata, created = await asyncio.wait_for(task, self.config.media.upload_timeout_seconds)
            logger.info("Device media id=%s device=%s bytes=%s created=%s", metadata["attachment_id"], device_id, metadata["size_bytes"], created)
            return metadata, created
        except asyncio.TimeoutError:
            raise MediaError(408, "upload_timeout", "Device upload timed out") from None
        except asyncio.CancelledError:
            if not self.authenticate_media(device_id, credential):
                raise MediaError(401, "device_unauthorized", "Device credential was revoked") from None
            if not self.accepting:
                raise MediaError(503, "device_unavailable", "Device media API is closing") from None
            raise
        finally:
            # Cancellation before execute's first instruction still releases
            # admission; running workers finish their disk cleanup first.
            self.uploads.pop(task, None)
            self._update_idle()

    async def resolve_attachments(self, connection: Connection, references: list[AttachmentReference]) -> list[dict]:
        device_id = connection.registration.device_id
        if self.devices.get(device_id) is not connection:
            raise MediaError(400, "device_session_changed", "Device session has changed")
        if not references:
            return []
        if self.media is None or device_id not in self.credentials.digests:
            raise MediaError(400, "invalid_attachment", "Device attachments are unavailable")
        owner = self.credentials.digests[device_id]
        if self._media_prepare is not None:
            await asyncio.shield(self._media_prepare)
        attachments = await self.media.resolve(device_id, owner, [str(ref.attachment_id) for ref in references])
        if self.devices.get(device_id) is not connection or self.credentials.digests.get(device_id) != owner:
            raise MediaError(400, "device_session_changed", "Device session has changed")
        return attachments

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
            row = {**registration.model_dump(exclude={"type"}), **self.profile_fields(key)}
            if location and row["location"] != location:
                continue
            if capability and capability not in {cap.name for cap in registration.capabilities}:
                continue
            rows.append(row)
        return {"status": "success", "devices": rows}

    def profile_fields(self, device_id: str) -> dict:
        owner = self.credentials.digests.get(device_id)
        return self.profiles.fields(device_id, owner) if owner else {}

    async def register(self, websocket, registration: Registration) -> Connection:
        if not self.accepting:
            raise RuntimeError("Device service is unavailable")
        if registration.device_id not in self.credentials.digests:
            raise ValueError("Device is not paired with this Agent")
        self._validate_registration(registration)
        self.profiles.reported(registration.device_id, self.credentials.digests[registration.device_id],
                               name=registration.name, location=registration.location)
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
        self._resolve(connection, result)

    async def resolve_result(self, connection: Connection, result: DeviceResult) -> None:
        pending = self.pending.get(result.command_id)
        if pending is None or pending.connection is not connection or not pending.sent or pending.future.done() or self.devices.get(connection.registration.device_id) is not connection:
            return
        try:
            attachments = await self.resolve_attachments(connection, result.attachments)
        except (MediaError, OSError, sqlite3.Error) as exc:
            self._resolve(connection, result, attachment_error=str(exc))
        else:
            self._resolve(connection, result, attachments=attachments)

    def _resolve(self, connection: Connection, result: DeviceResult, *, attachments=None, attachment_error=None) -> None:
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
        if attachments:
            response["attachments"] = attachments
        if attachment_error:
            response["attachment_error"] = {"code": "invalid_attachment", "message": attachment_error}
        elif result.attachments and attachments is None:
            response["attachment_error"] = {"code": "invalid_attachment", "message": "Attachments require asynchronous resolution"}
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
            timeout = request.timeout_seconds if request.timeout_seconds is not None else self.config.timeout_seconds
            response = await asyncio.wait_for(asyncio.shield(pending.future), timeout=timeout)
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
                self._update_idle()
        logger.info("Device command id=%s device=%s status=%s", command_id, request.device_id, response["status"])
        return response
