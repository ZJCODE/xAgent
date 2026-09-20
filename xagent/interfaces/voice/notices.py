"""Audible system lines for voice channel state and failures."""
from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .speech_text import sanitize_spoken_text

NoticeCategory = str


@dataclass(frozen=True)
class NoticeLine:
    category: NoticeCategory
    text: str
    tts_text: str = ""

    def spoken_text(self) -> str:
        return sanitize_spoken_text(self.tts_text or self.text)


@dataclass
class VoiceNoticeCatalog:
    """Rotate short system lines; never repeat the same line twice in a row."""

    lines: dict[NoticeCategory, list[NoticeLine]] = field(default_factory=dict)
    _last_spoken: dict[NoticeCategory, str] = field(default_factory=dict)
    _index: dict[NoticeCategory, int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def default(cls) -> "VoiceNoticeCatalog":
        return cls(
            lines={
                "not_understood": [
                    NoticeLine("not_understood", "Sorry, I didn't catch that."),
                    NoticeLine("not_understood", "I didn't hear you clearly. Could you say that again?"),
                ],
                "ears_offline": [
                    NoticeLine("ears_offline", "My ears are offline. I'll try reconnecting."),
                ],
                "back_online": [
                    NoticeLine("back_online", "I'm listening again."),
                    NoticeLine("back_online", "Ears are back online."),
                ],
                "still_working": [
                    NoticeLine("still_working", "Still working on that."),
                    NoticeLine("still_working", "One moment, I'm still on it."),
                ],
                "error": [
                    NoticeLine("error", "Something went wrong on my side."),
                    NoticeLine("error", "I hit an error. Let's try again."),
                ],
            }
        )

    def next_line(self, category: NoticeCategory) -> NoticeLine | None:
        options = self.lines.get(category) or []
        if not options:
            return None
        with self._lock:
            start = self._index.get(category, 0)
            for offset in range(len(options)):
                candidate = options[(start + offset) % len(options)]
                if candidate.text != self._last_spoken.get(category):
                    self._index[category] = (start + offset + 1) % len(options)
                    self._last_spoken[category] = candidate.text
                    return candidate
            chosen = options[start % len(options)]
            self._index[category] = (start + 1) % len(options)
            self._last_spoken[category] = chosen.text
            return chosen


class VoiceNoticeCache:
    """Cache synthesized notice audio on disk for offline TTS fallback."""

    def __init__(self, root: Path | str | None) -> None:
        self._root = Path(root).expanduser() if root else None

    def path_for(self, line: NoticeLine) -> Path | None:
        if self._root is None:
            return None
        digest = hashlib.sha256(line.spoken_text().encode("utf-8")).hexdigest()[:16]
        return self._root / f"{line.category}-{digest}.pcm"

    def read(self, line: NoticeLine) -> bytes | None:
        path = self.path_for(line)
        if path is None or not path.is_file():
            return None
        return path.read_bytes()

    def write(self, line: NoticeLine, audio: bytes) -> None:
        path = self.path_for(line)
        if path is None or not audio:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(audio)


class VoiceNoticeSpeaker:
    """Play or synthesize catalog lines."""

    def __init__(
        self,
        *,
        catalog: VoiceNoticeCatalog,
        cache: VoiceNoticeCache,
        synthesize: Callable[[str, str], Iterable[bytes]],
        play: Callable[[Iterable[bytes]], None],
        language_for: Callable[[], str],
    ) -> None:
        self.catalog = catalog
        self.cache = cache
        self._synthesize = synthesize
        self._play = play
        self._language_for = language_for

    def speak(self, category: NoticeCategory) -> bool:
        line = self.catalog.next_line(category)
        if line is None:
            return False
        spoken = line.spoken_text()
        cached = self.cache.read(line)
        if cached:
            self._play([cached])
            return True
        language = self._language_for()
        chunks = list(self._synthesize(spoken, language))
        if not chunks:
            return False
        audio = b"".join(chunks)
        self.cache.write(line, audio)
        self._play(chunks)
        return True
