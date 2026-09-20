"""Short thinking fillers before the first agent token arrives."""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field


DEFAULT_ACK_PHRASES = (
    "[thoughtful]嗯……",
    "[short]好。",
    "[thoughtful]让我想想。",
    "[short]OK.",
)


@dataclass
class InstantAckConfig:
    enabled: bool = True
    delay_ms: float = 400.0
    cooldown_seconds: float = 45.0
    phrases: list[str] = field(default_factory=lambda: list(DEFAULT_ACK_PHRASES))


class InstantAckSpeaker:
    """Pick a rare filler so every turn does not sound tic-like."""

    def __init__(self, config: InstantAckConfig) -> None:
        self.config = config
        self._last_spoken_at = 0.0

    def should_play(self) -> bool:
        if not self.config.enabled or self.config.delay_ms <= 0:
            return False
        cooldown = max(0.0, self.config.cooldown_seconds)
        if cooldown <= 0:
            return True
        return (time.monotonic() - self._last_spoken_at) >= cooldown

    def pick_phrase(self) -> str:
        choices = [phrase.strip() for phrase in self.config.phrases if phrase.strip()]
        if not choices:
            choices = list(DEFAULT_ACK_PHRASES)
        phrase = random.choice(choices)
        self._last_spoken_at = time.monotonic()
        return phrase
