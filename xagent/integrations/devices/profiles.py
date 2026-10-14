"""Agent-local device labels, kept separate from credentials and live manifests."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .config import DeviceId

Label = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=256)]
Description = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, max_length=2048)]


class ProfilePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Null restores the device's default; an empty string deliberately clears it.
    name: Label | None = None
    location: Label | None = None
    description: Description | None = None


class DeviceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str
    reported_name: str = ""
    reported_location: str = ""
    user: ProfilePatch = Field(default_factory=ProfilePatch)


class ProfileData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    devices: dict[DeviceId, DeviceProfile] = Field(default_factory=dict)


class DeviceProfiles:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.devices = ProfileData.model_validate_json(path.read_bytes()).devices if path and path.exists() else {}

    def get(self, device_id: str, owner: str) -> DeviceProfile | None:
        profile = self.devices.get(device_id)
        return profile if profile and profile.owner == owner else None

    def reported(self, device_id: str, owner: str, *, name: str, location: str) -> None:
        profile = self.get(device_id, owner) or DeviceProfile(owner=owner)
        updated = profile.model_copy(update={"reported_name": name, "reported_location": location})
        if updated != self.devices.get(device_id):
            self._save({**self.devices, device_id: updated})

    def update(self, device_id: str, owner: str, patch: ProfilePatch) -> None:
        profile = self.get(device_id, owner) or DeviceProfile(owner=owner)
        user = ProfilePatch.model_validate({**profile.user.model_dump(), **patch.model_dump(exclude_unset=True)})
        self._save({**self.devices, device_id: profile.model_copy(update={"user": user})})

    def fields(self, device_id: str, owner: str) -> dict:
        profile = self.get(device_id, owner)
        if profile is None:
            return {}
        return {
            "name": profile.user.name if profile.user.name is not None else profile.reported_name,
            "location": profile.user.location if profile.user.location is not None else profile.reported_location,
            "description": profile.user.description or "",
            "reported_name": profile.reported_name,
            "reported_location": profile.reported_location,
            "user_profile": profile.user.model_dump(exclude_none=True),
        }

    def _save(self, devices: dict[str, DeviceProfile]) -> None:
        payload = ProfileData(devices=devices).model_dump_json()
        if self.path is not None:
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
        # A failed write must not change the profile visible to the Agent.
        self.devices = devices
