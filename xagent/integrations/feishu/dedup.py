"""Persistent message dedup for the Feishu adapter.

The lark-oapi WebSocket transport can redeliver events after a reconnect, and
the SDK's in-process dedup window is lost when the Python process restarts.
This module ships a small file-backed ledger so the same Feishu
``message_id`` is never handled twice within the TTL window, even across
process restarts.

Design:

* In-memory ``_inflight`` set guards re-entrancy within a single process.
* In-memory ``_recent`` dict caches the last ``mem_max`` finalized ids for
  fast lookups; the on-disk JSON file is the durable source of truth.
* Both files and in-memory state hold ``{message_id: epoch_ms}`` entries.
* Expired entries are pruned on every write. The file is capped at
  ``file_max`` entries (oldest entries dropped first).
* Writes are atomic via ``os.replace`` to avoid torn files.

The module is intentionally synchronous: dedup IO is tiny (a few KB) and we
need it to be fast in the event-handler hot path.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Literal, Optional


ClaimResult = Literal["claimed", "duplicate", "inflight", "invalid"]


_DEFAULT_TTL_MS = 24 * 60 * 60 * 1000  # 24h
_DEFAULT_MEM_MAX = 1_000
_DEFAULT_FILE_MAX = 10_000

_SAFE_NAMESPACE_RE = re.compile(r"[^a-zA-Z0-9_.-]")


def _now_ms() -> int:
    return int(time.time() * 1000)


def default_state_dir() -> Path:
    """Resolve the default state directory.

    Honors the ``XAGENT_STATE_DIR`` env var so tests and packagers can pin
    a custom location.
    """
    override = os.environ.get("XAGENT_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".xagent"


class PersistentDedup:
    """File-backed, TTL-bounded message dedup ledger.

    A single instance is safe to share across coroutines on the same event
    loop; the internal lock is a plain ``threading.Lock`` because all IO
    is synchronous and short.
    """

    def __init__(
        self,
        *,
        namespace: str,
        state_dir: Optional[Path] = None,
        ttl_ms: int = _DEFAULT_TTL_MS,
        mem_max: int = _DEFAULT_MEM_MAX,
        file_max: int = _DEFAULT_FILE_MAX,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self._namespace = namespace
        self._ttl_ms = ttl_ms
        self._mem_max = mem_max
        self._file_max = file_max
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._recent: dict[str, int] = {}
        self._loaded = False

        base = state_dir or default_state_dir()
        safe_ns = _SAFE_NAMESPACE_RE.sub("_", namespace) or "default"
        self._path = base / "feishu" / "dedup" / f"{safe_ns}.json"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def try_begin(self, message_id: Optional[str]) -> ClaimResult:
        """Atomically claim a message for processing.

        Returns:
            ``"claimed"``   — caller owns this message and must
                              eventually call :meth:`finalize` or
                              :meth:`release`.
            ``"duplicate"`` — message was finalized previously.
            ``"inflight"`` — another coroutine in this process is still
                              handling it.
            ``"invalid"``   — empty / missing message id.
        """
        normalized = self._normalize(message_id)
        if normalized is None:
            return "invalid"
        with self._lock:
            self._ensure_loaded()
            if normalized in self._inflight:
                return "inflight"
            if self._is_recent_locked(normalized):
                return "duplicate"
            self._inflight.add(normalized)
            return "claimed"

    def finalize(self, message_id: Optional[str]) -> bool:
        """Mark a previously-claimed message as fully processed.

        Idempotent; safe to call from a ``finally`` block. Returns True
        when the message id was valid (regardless of whether it was
        actually claimed).
        """
        normalized = self._normalize(message_id)
        if normalized is None:
            return False
        with self._lock:
            self._ensure_loaded()
            self._inflight.discard(normalized)
            self._recent[normalized] = _now_ms()
            self._prune_locked()
            self._save_locked()
        return True

    def release(self, message_id: Optional[str]) -> None:
        """Release an inflight claim without recording it as processed.

        Use this when processing aborted before any user-visible action
        (e.g. validation failed) and the message should be retried on
        redelivery.
        """
        normalized = self._normalize(message_id)
        if normalized is None:
            return
        with self._lock:
            self._inflight.discard(normalized)

    def is_recent(self, message_id: Optional[str]) -> bool:
        """Return True iff the id was finalized within the TTL window."""
        normalized = self._normalize(message_id)
        if normalized is None:
            return False
        with self._lock:
            self._ensure_loaded()
            return self._is_recent_locked(normalized)

    @property
    def file_path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(message_id: Optional[str]) -> Optional[str]:
        if not isinstance(message_id, str):
            return None
        trimmed = message_id.strip()
        return trimmed or None

    def _is_recent_locked(self, normalized: str) -> bool:
        ts = self._recent.get(normalized)
        if ts is None:
            return False
        if _now_ms() - ts > self._ttl_ms:
            self._recent.pop(normalized, None)
            return False
        return True

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if not self._path.is_file():
                return
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning(
                "feishu-dedup: failed to load %s (%s); starting empty",
                self._path,
                exc,
            )
            return
        if not isinstance(payload, dict):
            return
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return
        cutoff = _now_ms() - self._ttl_ms
        for key, value in entries.items():
            if not isinstance(key, str):
                continue
            if not isinstance(value, (int, float)):
                continue
            ts = int(value)
            if ts >= cutoff:
                self._recent[key] = ts
        # Cap memory cache to mem_max most-recent entries.
        if len(self._recent) > self._mem_max:
            keep = sorted(self._recent.items(), key=lambda kv: kv[1], reverse=True)
            self._recent = dict(keep[: self._mem_max])

    def _prune_locked(self) -> None:
        cutoff = _now_ms() - self._ttl_ms
        if self._recent:
            self._recent = {k: v for k, v in self._recent.items() if v >= cutoff}
        if len(self._recent) > self._file_max:
            keep = sorted(self._recent.items(), key=lambda kv: kv[1], reverse=True)
            self._recent = dict(keep[: self._file_max])

    def _save_locked(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"namespace": self._namespace, "entries": self._recent}
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                dir=str(self._path.parent),
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle, separators=(",", ":"))
                os.replace(tmp_path, self._path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self._logger.warning(
                "feishu-dedup: failed to persist %s (%s); state kept in memory only",
                self._path,
                exc,
            )
