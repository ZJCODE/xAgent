"""Room presence and STT session sleep/wake heuristics."""
from __future__ import annotations

import audioop
import struct
import threading
import time
from dataclasses import dataclass


def pcm16_rms(chunk: bytes) -> float:
    if not chunk:
        return 0.0
    if len(chunk) % 2 == 1:
        chunk = chunk[:-1]
    if not chunk:
        return 0.0
    try:
        return float(audioop.rms(chunk, 2))
    except Exception:
        sample_count = len(chunk) // 2
        samples = struct.unpack(f"<{sample_count}h", chunk)
        if not samples:
            return 0.0
        mean_sq = sum(sample * sample for sample in samples) / len(samples)
        return mean_sq**0.5


@dataclass
class VoicePresenceConfig:
    close_stt_after_idle_seconds: float = 0.0
    wake_energy_rms: float = 450.0
    recent_speech_hours: float = 6.0


class SttLifecycleController:
    """Close the STT websocket when the room is quiet; reopen on loud audio."""

    def __init__(self, config: VoicePresenceConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._sleeping = False
        self._wake = threading.Event()
        self._wake.set()
        self._last_activity = time.monotonic()
        self._last_heard_endpoint = time.monotonic()

    @property
    def enabled(self) -> bool:
        return self.config.close_stt_after_idle_seconds > 0

    def note_endpoint(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._last_activity = now
            self._last_heard_endpoint = now
            self._sleeping = False
        self._wake.set()

    def observe_audio(self, chunk: bytes) -> None:
        if not self.enabled or not chunk:
            return
        if pcm16_rms(chunk) >= self.config.wake_energy_rms:
            now = time.monotonic()
            with self._lock:
                self._last_activity = now
                self._sleeping = False
            self._wake.set()

    def should_close_session(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            idle_for = time.monotonic() - self._last_activity
            return idle_for >= self.config.close_stt_after_idle_seconds

    def enter_sleep(self) -> None:
        with self._lock:
            self._sleeping = True
        self._wake.clear()

    def is_sleeping(self) -> bool:
        with self._lock:
            return self._sleeping

    def wait_until_awake(self, stop_event: threading.Event, *, poll_seconds: float = 0.2) -> None:
        while not stop_event.is_set():
            if self._wake.wait(timeout=poll_seconds):
                return

    def heard_recently(self) -> bool:
        hours = max(0.0, self.config.recent_speech_hours)
        if hours <= 0:
            return True
        cutoff = time.monotonic() - hours * 3600.0
        with self._lock:
            return self._last_heard_endpoint >= cutoff
