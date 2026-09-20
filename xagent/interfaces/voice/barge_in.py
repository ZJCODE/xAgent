"""Classify whether incoming speech should interrupt agent playback."""
from __future__ import annotations

import re
from dataclasses import dataclass

_BACKCHANNELS = frozenset(
    {
        "嗯",
        "嗯嗯",
        "对",
        "对对",
        "好",
        "好的",
        "ok",
        "okay",
        "yeah",
        "yep",
        "uh-huh",
        "uh huh",
        "mm",
        "mm-hmm",
    }
)


def count_words(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    cjk = len(re.findall(r"[\u4e00-\u9fff]", stripped))
    latin = len(re.findall(r"[A-Za-z]+", stripped))
    if cjk and not latin:
        return max(1, cjk)
    if latin and not cjk:
        return len(re.findall(r"[A-Za-z]+", stripped))
    return max(1, cjk + len(re.findall(r"[A-Za-z]+", stripped)))


@dataclass
class BargeInConfig:
    min_speech_ms: int = 250
    min_words: int = 2


@dataclass
class BargeInState:
    speech_ms: float = 0.0
    armed: bool = False


class BargeInEvaluator:
    def __init__(self, config: BargeInConfig | None = None) -> None:
        self.config = config or BargeInConfig()
        self._state = BargeInState()

    def reset(self) -> None:
        self._state = BargeInState()

    def observe_vad(self, *, active: bool, block_ms: float) -> None:
        if active:
            self._state.speech_ms += block_ms
            if self._state.speech_ms >= self.config.min_speech_ms:
                self._state.armed = True
        else:
            self._state.speech_ms = 0.0
            self._state.armed = False

    def evaluate_partial(self, partial_text: str) -> str:
        """Return ``none``, ``backchannel``, or ``interrupt``."""
        if not self._state.armed:
            return "none"
        text = partial_text.strip().lower()
        if not text:
            return "none"
        normalized = text.replace(" ", "")
        if normalized in _BACKCHANNELS or text in _BACKCHANNELS:
            return "backchannel"
        if count_words(partial_text) < self.config.min_words:
            return "none"
        return "interrupt"
