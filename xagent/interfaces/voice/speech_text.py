"""Spoken-output helpers: channel instructions, streaming sanitizer, reply language."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

VOICE_CHANNEL_INSTRUCTIONS = (
    "You are speaking aloud on a voice channel. The listener cannot see text, scroll back, "
    "or open attachments.\n"
    "- One idea per turn. Two or three short sentences unless the user explicitly asks for more.\n"
    "- No lists, headings, markup, code, tables, or emoji. If the answer is inherently a list, "
    "give the headline and offer to go through items one at a time.\n"
    "- Do not read URLs, file paths, or IDs aloud; offer to send them on a text channel instead.\n"
    "- Do not use attach_artifact or structured file delivery; offer to send files elsewhere.\n"
    "- Speak numbers, dates, times, and units the way a person says them.\n"
    "- Do not announce structure (for example \"three things:\"); the listener cannot see numbering.\n"
    "- End with a question only when you actually want the floor back.\n"
    "- Never mention the channel, the microphone, or that you cannot show files."
)

_STREAM_HOLD_CHARS = 8
_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E0-\U0001F1FF"
    "]+",
    flags=re.UNICODE,
)
def detect_dominant_language(text: str, *, hints: list[str], fallback: str) -> str:
    """Pick a Soniox TTS language code from reply text."""
    normalized_hints = [item.strip() for item in hints if item.strip()]
    if not normalized_hints:
        normalized_hints = [fallback]
    if not text.strip():
        return fallback.strip() or normalized_hints[0]

    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    if cjk > latin:
        if "zh" in normalized_hints:
            return "zh"
        for hint in normalized_hints:
            if hint.startswith("zh"):
                return hint
    if latin > cjk:
        if "en" in normalized_hints:
            return "en"
        for hint in normalized_hints:
            if hint.startswith("en"):
                return hint
    return fallback.strip() or normalized_hints[0]


@dataclass
class ConversationLanguageTracker:
    """Track stable TTS language from agent replies, not user STT language."""

    fallback: str
    hints: list[str] = field(default_factory=list)
    current: str = ""
    _candidate: str = ""
    _candidate_streak: int = 0

    def __post_init__(self) -> None:
        self.current = (self.current or self.fallback or "en").strip()

    def language_before_turn(self) -> str:
        return self.current

    def language_for_streaming(self, partial_reply: str) -> str:
        detected = detect_dominant_language(
            partial_reply,
            hints=self.hints,
            fallback=self.current or self.fallback,
        )
        if detected:
            return detected
        return self.current

    def observe_reply(self, reply: str) -> str:
        detected = detect_dominant_language(
            reply,
            hints=self.hints,
            fallback=self.current or self.fallback,
        )
        if detected == self.current:
            self._candidate = ""
            self._candidate_streak = 0
            return self.current
        if detected == self._candidate:
            self._candidate_streak += 1
        else:
            self._candidate = detected
            self._candidate_streak = 1
        if self._candidate_streak >= 2:
            self.current = self._candidate
            self._candidate = ""
            self._candidate_streak = 0
        return self.current


class StreamingTTSSanitizer:
    """Strip text-channel artefacts from streamed LLM deltas before TTS."""

    def __init__(self, *, hold_chars: int = _STREAM_HOLD_CHARS) -> None:
        self._hold_chars = max(0, hold_chars)
        self._buffer = ""

    def feed(self, chunk: str) -> list[str]:
        if not chunk:
            return []
        self._buffer += chunk
        outputs: list[str] = []
        while self._buffer:
            hold = _hold_suffix_len(self._buffer, max_hold=self._hold_chars)
            if hold >= len(self._buffer):
                break
            emit = self._buffer[: len(self._buffer) - hold]
            self._buffer = self._buffer[len(self._buffer) - hold :]
            cleaned = _clean_spoken_fragment(emit, final=False)
            if cleaned:
                outputs.append(cleaned)
        return outputs

    def flush(self) -> list[str]:
        cleaned = _clean_spoken_fragment(self._buffer, final=True)
        self._buffer = ""
        return [cleaned] if cleaned else []


def sanitize_spoken_text(text: str) -> str:
    """One-shot sanitizer for fixed strings (scheduled tasks, notices)."""
    return _clean_spoken_fragment(text, final=True)


def _clean_spoken_fragment(text: str, *, final: bool = True) -> str:
    if not text:
        return ""
    cleaned = text.replace("```", " ")
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"\*\*([^*]+)\*\*", r"\1", cleaned)
    cleaned = re.sub(r"\*([^*]+)\*", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*[-*+]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r"^\s*\d+[.)]\s+", "", cleaned, flags=re.MULTILINE)
    cleaned = _URL_PATTERN.sub("链接", cleaned)
    cleaned = _EMOJI_PATTERN.sub("", cleaned)
    cleaned = cleaned.replace("*", "").replace("_", "").replace("`", "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    if final:
        return cleaned.strip()
    return cleaned.lstrip()


def _hold_suffix_len(text: str, *, max_hold: int) -> int:
    """Keep a short suffix only while it may complete a markdown or URL token."""
    limit = min(max_hold, len(text))
    for size in range(limit, 0, -1):
        if _incomplete_spoken_suffix(text[-size:]):
            return size
    return 0


def _incomplete_spoken_suffix(text: str) -> bool:
    if not text:
        return False
    if text.endswith(("http://", "https://", "www.")):
        return True
    if text.count("`") % 2 == 1:
        return True
    if text.endswith("*") or text.endswith("_"):
        return True
    if text.endswith("[") or text.endswith("]("):
        return True
    if text.endswith(("ht", "ttp", "://")):
        return True
    return False
