"""Responses-layer memory helpers."""

from __future__ import annotations

from typing import Any, List, Optional


class ResponsesMemoryManager:
    def __init__(self, memory_storage: Any):
        self.memory_storage = memory_storage

    async def retrieve(self, user_id: str, query: str, limit: int = 5) -> List[Any]:
        if self.memory_storage is None:
            return []
        return await self.memory_storage.retrieve(user_id=user_id, query=query, limit=limit)

    async def add_messages(self, user_id: str, messages: List[Any]) -> None:
        if self.memory_storage is None:
            return
        await self.memory_storage.add(user_id=user_id, messages=messages)
