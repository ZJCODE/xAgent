"""Guards for unprompted voice output (scheduled tasks, subconscious)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ProactiveSpeechLimiter:
    max_per_hour: int
    _timestamps: list[float] = field(default_factory=list)

    def allow(self) -> bool:
        limit = max(0, self.max_per_hour)
        if limit == 0:
            return False
        self._prune()
        return len(self._timestamps) < limit

    def record(self) -> None:
        self._prune()
        self._timestamps.append(time.monotonic())

    def _prune(self) -> None:
        cutoff = time.monotonic() - 3600.0
        self._timestamps = [stamp for stamp in self._timestamps if stamp >= cutoff]
