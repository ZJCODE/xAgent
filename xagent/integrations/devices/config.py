"""Agent-local configuration for external execution devices."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


DeviceId = Annotated[str, StringConstraints(
    min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]


class DeviceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, hide_input_in_errors=True)
    timeout_seconds: float = Field(default=10, ge=0.1, le=120, strict=True)
