"""Minimal message storage for adapter tests using the attention/respond path."""

from __future__ import annotations

from typing import List, Optional

from xagent.core.config import AgentConfig
from xagent.schemas import Message


class InMemoryRoomMessageStorage:
    def __init__(self) -> None:
        self.messages: List[Message] = []
        self._seq = 0

    async def add_messages(self, messages, **kwargs):
        items = messages if isinstance(messages, list) else [messages]
        stored: List[Message] = []
        for item in items:
            self._seq += 1
            copy = item.model_copy(deep=True)
            copy.metadata = dict(copy.metadata or {})
            copy.metadata[AgentConfig.MESSAGE_STORAGE_CURSOR_KEY] = self._seq
            self.messages.append(copy)
            stored.append(copy)
        return stored

    async def get_messages_for_room(
        self,
        room_key,
        start_exclusive=0,
        end_inclusive=None,
        limit=None,
    ):
        matched = []
        for message in self.messages:
            if str((message.metadata or {}).get("room_key") or "") != room_key:
                continue
            cursor = int((message.metadata or {}).get("storage_cursor") or 0)
            if cursor <= start_exclusive:
                continue
            if end_inclusive is not None and cursor > end_inclusive:
                continue
            matched.append(message)
        if limit is not None:
            matched = matched[-limit:]
        return matched

    async def get_latest_room_cursor(self, room_key: str) -> int:
        cursors = [
            int((message.metadata or {}).get("storage_cursor") or 0)
            for message in self.messages
            if str((message.metadata or {}).get("room_key") or "") == room_key
        ]
        return max(cursors) if cursors else 0

    async def list_room_keys(self) -> List[str]:
        keys = {
            str((message.metadata or {}).get("room_key") or "").strip()
            for message in self.messages
        }
        return sorted(key for key in keys if key)
