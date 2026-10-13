"""Agent-local configuration for external execution devices."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


DeviceId = Annotated[str, StringConstraints(
    min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]

CommandTimeout = Annotated[float, Field(ge=0.1, le=120, strict=True)]


class DeviceMediaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0, strict=True)
    max_file_bytes: int = Field(default=50 * 1024 * 1024, gt=0, strict=True)
    max_attachments: int = Field(default=8, ge=1, le=128, strict=True)
    max_message_bytes: int = Field(default=200 * 1024 * 1024, gt=0, strict=True)
    max_device_uploads: int = Field(default=1, ge=1, strict=True)
    max_uploads: int = Field(default=4, ge=1, strict=True)
    upload_timeout_seconds: float = Field(default=60, gt=0, strict=True)
    idle_timeout_seconds: float = Field(default=15, gt=0, strict=True)
    device_quota_bytes: int = Field(default=1024 * 1024 * 1024, gt=0, strict=True)
    quota_bytes: int = Field(default=5 * 1024 * 1024 * 1024, gt=0, strict=True)


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    timeout_seconds: CommandTimeout = 10
    media: DeviceMediaConfig = Field(default_factory=DeviceMediaConfig)
