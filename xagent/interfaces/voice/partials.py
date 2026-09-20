"""Thread-safe relay for live STT partial transcripts."""
from __future__ import annotations

import threading


class PartialTranscriptRelay:
    """Latest non-final transcript text from an STT session."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._text = ""

    def update(self, text: str) -> None:
        with self._lock:
            self._text = text

    def snapshot(self) -> str:
        with self._lock:
            return self._text

    def clear(self) -> None:
        with self._lock:
            self._text = ""
