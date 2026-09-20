"""Structured per-turn voice metrics written for offline analysis."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VoiceTurnMetrics:
    """One completed or failed voice turn."""

    transcript: str = ""
    tts_language: str = ""
    endpoint_at: float = 0.0
    first_text_at: float | None = None
    first_audio_at: float | None = None
    turn_end_at: float | None = None
    reply_char_count: int = 0
    playback_audio_bytes: int = 0
    interrupted: bool = False
    error_class: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def endpoint_to_first_text_ms(self) -> float | None:
        if self.first_text_at is None:
            return None
        return (self.first_text_at - self.endpoint_at) * 1000

    def endpoint_to_first_audio_ms(self) -> float | None:
        if self.first_audio_at is None:
            return None
        return (self.first_audio_at - self.endpoint_at) * 1000

    def turn_total_ms(self) -> float | None:
        if self.turn_end_at is None:
            return None
        return (self.turn_end_at - self.endpoint_at) * 1000

    def to_record(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["recorded_at_unix"] = time.time()
        payload["endpoint_to_first_text_ms"] = self.endpoint_to_first_text_ms()
        payload["endpoint_to_first_audio_ms"] = self.endpoint_to_first_audio_ms()
        payload["turn_total_ms"] = self.turn_total_ms()
        return payload


class VoiceTurnMetricsWriter:
    """Append JSON lines under the runtime directory."""

    def __init__(self, path: Path | str | None) -> None:
        self._path = Path(path).expanduser() if path else None

    def write(self, metrics: VoiceTurnMetrics) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(metrics.to_record(), ensure_ascii=False)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
