"""Small Agent-local credential store; never persists device manifests."""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import DeviceId


class CredentialData(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    devices: dict[DeviceId, Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]] = Field(default_factory=dict)
    reusable_pairing_digest: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None = None


class DeviceCredentials:
    def __init__(self, path: Path | None = None):
        self.path = path
        data = CredentialData.model_validate_json(path.read_bytes()) if path is not None and path.exists() else CredentialData()
        self.digests = data.devices
        self.reusable_pairing_digest = data.reusable_pairing_digest

    def authenticate_reusable_pairing(self, token: str) -> bool:
        return self.reusable_pairing_digest is not None and secrets.compare_digest(
            self.reusable_pairing_digest, self._digest("", token),
        )

    def set_reusable_pairing(self, token: str | None) -> None:
        digest = self._digest("", token) if token is not None else None
        self._write(self.digests, digest)
        self.reusable_pairing_digest = digest

    @staticmethod
    def _digest(device_id: str, token: str) -> str:
        return hashlib.sha256((device_id + "\0" + token).encode("utf-8")).hexdigest()

    def authenticate(self, device_id: str, token: str) -> bool:
        stored = self.digests.get(device_id)
        return stored is not None and secrets.compare_digest(stored, self._digest(device_id, token))

    def add(self, device_id: str, token: str) -> None:
        if device_id in self.digests:
            raise ValueError("Device ID is already paired; remove it before pairing again")
        updated = {**self.digests, device_id: self._digest(device_id, token)}
        self._write(updated, self.reusable_pairing_digest)
        self.digests = updated

    def revoke(self, device_id: str) -> None:
        updated = {key: value for key, value in self.digests.items() if key != device_id}
        self._write(updated, self.reusable_pairing_digest)
        self.digests = updated

    def _write(self, devices: dict[str, str], reusable_pairing_digest: str | None) -> None:
        payload = CredentialData(devices=devices, reusable_pairing_digest=reusable_pairing_digest).model_dump_json(exclude_none=True)
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
