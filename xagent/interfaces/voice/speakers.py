"""Map Soniox session speaker labels to persistent user ids."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpeakerAttribution:
    speaker_label: str
    user_id: str
    bound: bool
    confidence: float = 0.0


class SpeakerBindingStore:
    """Persist label→user_id hints for the local voice room."""

    def __init__(self, path: Path | str | None) -> None:
        self._path = Path(path).expanduser() if path else None
        self._lock = threading.Lock()
        self._labels: dict[str, str] = {}
        self._load()

    def resolve(self, *, speaker_label: str, fallback_user_id: str, confidence: float = 0.0) -> SpeakerAttribution:
        label = speaker_label.strip()
        if not label:
            return SpeakerAttribution("", fallback_user_id, bound=False, confidence=confidence)
        with self._lock:
            bound_id = self._labels.get(label)
        if bound_id:
            return SpeakerAttribution(label, bound_id, bound=True, confidence=confidence)
        provisional = f"{fallback_user_id}#{label}"
        return SpeakerAttribution(label, provisional, bound=False, confidence=confidence)

    def bind(self, speaker_label: str, user_id: str) -> None:
        label = speaker_label.strip()
        target = user_id.strip()
        if not label or not target:
            return
        with self._lock:
            self._labels[label] = target
            self._save()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        labels = payload.get("labels") if isinstance(payload, dict) else None
        if isinstance(labels, dict):
            self._labels = {str(k): str(v) for k, v in labels.items() if str(k).strip() and str(v).strip()}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"labels": dict(self._labels)}
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
