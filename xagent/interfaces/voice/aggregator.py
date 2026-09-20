"""Merge Soniox endpoint segments into one user turn when speech continues."""
from __future__ import annotations

import queue
import threading
import time
from collections import Counter
from typing import Iterable, Iterator

from .types import VoiceUtterance

_DEFAULT_GRACE_SECONDS = 0.35
_EXTENDED_GRACE_SECONDS = 0.55
_SHORT_GRACE_SECONDS = 0.28
_POLL_INTERVAL_SECONDS = 0.05
_MAX_SEGMENTS = 12

_TRAILING_CONTINUE = (
    "而且",
    "然后",
    "所以",
    "因为",
    "还有",
    "以及",
    "uh",
    "um",
    "and",
    "but",
    "so",
)


def iter_aggregated_utterances(
    utterances: Iterable[VoiceUtterance],
    *,
    stop_event: threading.Event,
    max_segments: int = _MAX_SEGMENTS,
) -> Iterator[VoiceUtterance]:
    """Yield merged utterances after a short grace window following each endpoint."""
    feeder = _UtteranceFeeder(utterances)
    try:
        while not stop_event.is_set():
            first = feeder.get(timeout=_POLL_INTERVAL_SECONDS)
            if first is None:
                if feeder.done:
                    return
                continue
            batch = [first]
            deadline = time.monotonic() + _grace_seconds_for(first.text)
            while len(batch) < max_segments and not stop_event.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                nxt = feeder.get(timeout=min(_POLL_INTERVAL_SECONDS, remaining))
                if nxt is None:
                    continue
                batch.append(nxt)
                deadline = time.monotonic() + _grace_seconds_for(nxt.text)
            yield _merge_utterances(batch)
    finally:
        feeder.close()


def _grace_seconds_for(text: str) -> float:
    stripped = text.strip()
    if not stripped:
        return _DEFAULT_GRACE_SECONDS
    lowered = stripped.lower()
    for suffix in _TRAILING_CONTINUE:
        if lowered.endswith(suffix.lower()) or stripped.endswith(suffix):
            return _EXTENDED_GRACE_SECONDS
    if stripped.endswith(("?", "？")) or stripped.endswith(("吗", "呢")):
        return _SHORT_GRACE_SECONDS
    if len(stripped) <= 12:
        return _SHORT_GRACE_SECONDS
    return _DEFAULT_GRACE_SECONDS


def _merge_utterances(parts: list[VoiceUtterance]) -> VoiceUtterance:
    text = " ".join(part.text.strip() for part in parts if part.text.strip()).strip()
    languages = Counter(part.language for part in parts if part.language)
    language = languages.most_common(1)[0][0] if languages else ""
    speakers = Counter(part.speaker_label for part in parts if part.speaker_label)
    speaker_label = speakers.most_common(1)[0][0] if speakers else ""
    confidences = [part.confidence for part in parts if part.confidence]
    confidence = min(confidences) if confidences else 0.0
    return VoiceUtterance(
        text=text,
        language=language,
        speaker_label=speaker_label,
        confidence=confidence,
    )


class _UtteranceFeeder:
    """Bridge a blocking recognizer iterator into timed aggregation waits."""

    def __init__(self, utterances: Iterable[VoiceUtterance]) -> None:
        self._queue: queue.Queue[VoiceUtterance | object] = queue.Queue()
        self._done = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(utterances,),
            daemon=True,
            name="xagent-voice-utterance-feed",
        )
        self._thread.start()

    @property
    def done(self) -> bool:
        return self._done.is_set() and self._queue.empty()

    def close(self) -> None:
        self._stop.set()

    def get(self, *, timeout: float) -> VoiceUtterance | None:
        if timeout <= 0:
            return None
        try:
            item = self._queue.get(timeout=timeout)
        except queue.Empty:
            return None
        if item is _STOP:
            return None
        return item

    def _run(self, utterances: Iterable[VoiceUtterance]) -> None:
        try:
            for utterance in utterances:
                if self._stop.is_set():
                    break
                self._queue.put(utterance)
        finally:
            self._queue.put(_STOP)
            self._done.set()


_STOP = object()
