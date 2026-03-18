import asyncio
from typing import List

from ...components import MemoryStorageBase, MessageStorageBase


class MemoryManager:
    """Manages lightweight memory retrieval and background persistence."""

    def __init__(
        self,
        memory_storage: MemoryStorageBase,
        message_storage: MessageStorageBase,
    ):
        self.memory_storage = memory_storage
        self._background_tasks: set[asyncio.Task] = set()

        if self.memory_storage.message_storage is None:
            self.memory_storage.message_storage = message_storage

    async def retrieve_memories(
        self,
        memory_key: str,
        query: str,
        limit: int = 5,
    ) -> list:
        return await self.memory_storage.retrieve(
            memory_key=memory_key,
            query=query,
            limit=limit,
        )

    def schedule_memory_add(
        self,
        memory_key: str,
        messages: List[dict],
    ) -> None:
        if not messages:
            return

        task = asyncio.create_task(
            self.memory_storage.add(
                memory_key=memory_key,
                messages=messages,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
