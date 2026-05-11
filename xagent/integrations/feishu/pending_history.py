"""In-memory pending group history for the Feishu adapter.

When the Feishu app lacks ``im:message:readonly`` (or even when it has it),
calling ``im.v1.message.alist`` on every @-mention is wasteful and slow.
This store caches every group message the adapter observes — both those
routed to ``agent.observe`` and those routed to ``agent.chat`` — so the
next @-mention can be primed with the recent conversation for free.

The store is bounded (``max_per_chat`` per ``chat_id``, ``ttl_seconds``
sliding window) and process-local; persistence is intentionally out of
scope.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional


@dataclass(frozen=True)
class PendingHistoryEntry:
    """A single recorded group message."""

    message_id: Optional[str]
    sender_id: str
    sender_name: Optional[str]
    text: str
    timestamp_ms: int


class PendingHistoryStore:
    """Thread-safe ring buffer of recent group messages keyed by chat id."""

    def __init__(self, *, max_per_chat: int = 20, ttl_seconds: float = 30 * 60) -> None:
        self._max_per_chat = max(1, int(max_per_chat))
        self._ttl_ms = int(ttl_seconds * 1000)
        self._lock = threading.Lock()
        self._buffers: Dict[str, Deque[PendingHistoryEntry]] = {}

    def record(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        sender_id: str,
        sender_name: Optional[str],
        text: str,
        timestamp_ms: Optional[int] = None,
    ) -> None:
        """Append a message to the chat's buffer (no-op for empty text)."""
        cleaned = (text or "").strip()
        if not cleaned or not chat_id:
            return
        ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
        entry = PendingHistoryEntry(
            message_id=message_id,
            sender_id=sender_id or "unknown",
            sender_name=sender_name,
            text=cleaned,
            timestamp_ms=ts,
        )
        with self._lock:
            buf = self._buffers.get(chat_id)
            if buf is None:
                buf = deque(maxlen=self._max_per_chat)
                self._buffers[chat_id] = buf
            # Dedup by message_id within the buffer.
            if entry.message_id:
                for existing in buf:
                    if existing.message_id == entry.message_id:
                        return
            buf.append(entry)

    def peek(
        self,
        *,
        chat_id: str,
        exclude_message_id: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> List[PendingHistoryEntry]:
        """Return a chronologically-ordered snapshot for the chat.

        Expired entries are pruned. The triggering message (when supplied)
        is excluded so the agent's "context recap" never echoes the
        message it is about to reply to.
        """
        cutoff = int(time.time() * 1000) - self._ttl_ms
        with self._lock:
            buf = self._buffers.get(chat_id)
            if not buf:
                return []
            fresh = [entry for entry in buf if entry.timestamp_ms >= cutoff]
            if len(fresh) != len(buf):
                buf.clear()
                buf.extend(fresh)
            snapshot = [e for e in fresh if e.message_id != exclude_message_id]
        if limit is not None and limit >= 0:
            snapshot = snapshot[-limit:]
        return snapshot

    def clear(self, chat_id: str) -> None:
        """Drop the buffer for a chat (e.g. after a successful reply)."""
        with self._lock:
            self._buffers.pop(chat_id, None)
