"""Catch the agent hearing its own playback as a barge-in.

Barge-in is enabled from device detection, and detection can be wrong: a
speakerphone that does not actually cancel echo looks like one that does. The
symptom is specific enough to recognise — the words that "interrupt" the agent
are the words the agent is saying — so treat it as evidence and stop, rather
than letting the agent argue with itself all session.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

_ECHO_OVERLAP_RATIO = 0.6
_MIN_COMPARABLE_CHARS = 4
_STRIP_PATTERN = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


def _normalize(text: str) -> str:
    return _STRIP_PATTERN.sub("", str(text or "").lower())


def looks_like_echo(partial: str, spoken_text: str) -> bool:
    """True when an interrupting partial is mostly the agent's own words."""
    heard = _normalize(partial)
    said = _normalize(spoken_text)
    if len(heard) < _MIN_COMPARABLE_CHARS or not said:
        return False
    match = SequenceMatcher(None, heard, said, autojunk=False).find_longest_match(
        0, len(heard), 0, len(said)
    )
    return match.size / len(heard) >= _ECHO_OVERLAP_RATIO


@dataclass
class SelfInterruptionGuard:
    """Disable barge-in once the microphone has proven it hears the speaker."""

    max_echoes: int = 2
    echoes: int = 0
    tripped: bool = False

    def classify(self, partial: str, spoken_text: str) -> bool:
        """Record and report whether this barge-in attempt was our own audio."""
        if not looks_like_echo(partial, spoken_text):
            self.echoes = 0
            return False
        self.echoes += 1
        if self.echoes >= self.max_echoes:
            self.tripped = True
        return True
