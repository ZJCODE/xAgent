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


class DeviceCredentials:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.digests = CredentialData.model_validate_json(path.read_bytes()).devices if path is not None and path.exists() else {}

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
        self._write(updated)
        self.digests = updated

    def revoke(self, device_id: str) -> None:
        updated = {key: value for key, value in self.digests.items() if key != device_id}
        self._write(updated)
        self.digests = updated

    def _write(self, devices: dict[str, str]) -> None:
        payload = CredentialData(devices=devices).model_dump_json()
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
