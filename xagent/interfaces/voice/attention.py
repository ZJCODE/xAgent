"""Decide whether room speech is addressed to the agent."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


def normalize_terms(terms: list[str]) -> list[str]:
    cleaned = [term.strip() for term in terms if term and term.strip()]
    return list(dict.fromkeys(cleaned))


def transcript_mentions_term(transcript: str, terms: list[str]) -> bool:
    text = transcript.strip()
    if not text or not terms:
        return False
    lowered = text.lower()
    for term in terms:
        candidate = term.strip()
        if not candidate:
            continue
        if candidate.lower() in lowered:
            return True
        if re.search(rf"\b{re.escape(candidate.lower())}\b", lowered):
            return True
    return False


@dataclass
class VoiceAttentionConfig:
    open_window_seconds: float = 25.0
    wake_terms: list[str] = field(default_factory=list)
    use_decide_participation: bool = True


@dataclass
class VoiceAttentionGate:
    """Three-tier attention: open window, wake term, else LLM gate."""

    config: VoiceAttentionConfig
    wake_terms: list[str] = field(default_factory=list)
    _window_open_until: float = 0.0

    def __post_init__(self) -> None:
        self.wake_terms = normalize_terms(list(self.config.wake_terms) + list(self.wake_terms))
        if self.config.open_window_seconds > 0:
            self._window_open_until = time.monotonic() + self.config.open_window_seconds

    def mark_dispatched(self) -> None:
        self._window_open_until = time.monotonic() + max(0.0, self.config.open_window_seconds)

    def attention_tier(self, transcript: str) -> int:
        if time.monotonic() <= self._window_open_until:
            return 1
        if transcript_mentions_term(transcript, self.wake_terms):
            return 2
        return 3

    def needs_participation_decision(self, transcript: str) -> bool:
        return self.attention_tier(transcript) >= 3 and self.config.use_decide_participation
