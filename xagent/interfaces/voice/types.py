"""Shared voice channel datatypes."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VoiceUtterance:
    """A completed user turn returned by Soniox endpoint detection."""

    text: str
    language: str = ""
    speaker_label: str = ""
    confidence: float = 0.0
