"""Map TTS character timestamps to audible playback position."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SpokenLedger:
    """Track which generated characters were likely heard in the room."""

    sample_rate: int = 24_000
    channels: int = 1
    bytes_per_sample: int = 2
    playback_seconds: float = 0.0
    _characters: list[str] = field(default_factory=list)
    _char_end_times: list[float] = field(default_factory=list)

    def reset(self) -> None:
        self.playback_seconds = 0.0
        self._characters.clear()
        self._char_end_times.clear()

    def ingest_timestamps(
        self,
        *,
        characters: list[str],
        character_end_times_seconds: list[float],
    ) -> None:
        if not characters or not character_end_times_seconds:
            return
        limit = min(len(characters), len(character_end_times_seconds))
        for index in range(limit):
            self._characters.append(characters[index])
            self._char_end_times.append(float(character_end_times_seconds[index]))

    def note_playback_bytes(self, chunk_size: int) -> None:
        if chunk_size <= 0:
            return
        frame_bytes = self.bytes_per_sample * self.channels
        frames = chunk_size / frame_bytes
        self.playback_seconds += frames / float(self.sample_rate)

    def spoken_char_index(self) -> int:
        if not self._char_end_times:
            return 0
        audible = self.playback_seconds
        spoken = 0
        for index, end_time in enumerate(self._char_end_times):
            if end_time > audible:
                break
            spoken += len(self._characters[index])
        return spoken

    def spoken_prefix(self, full_text: str) -> str:
        if not full_text:
            return ""
        index = self.spoken_char_index()
        if index <= 0:
            return ""
        return full_text[:index]

    def spoken_through_metadata(self, full_text: str) -> dict[str, object]:
        prefix = self.spoken_prefix(full_text)
        return {
            "voice": {
                "spoken_through_chars": len(prefix),
                "spoken_through_text": prefix,
                "playback_seconds": round(self.playback_seconds, 3),
                "generated_chars": len(full_text),
            }
        }
