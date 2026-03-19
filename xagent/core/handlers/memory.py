import asyncio
import logging
from datetime import datetime, timedelta
from typing import List

from ...components import MemoryStorageBase, MessageStorageBase


class MemoryManager:
    """Manages journal retrieval and background persistence."""

    ALWAYS_INCLUDE_RECENT_DAYS = 2

    def __init__(
        self,
        memory_storage: MemoryStorageBase,
        message_storage: MessageStorageBase,
    ):
        self.memory_storage = memory_storage
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        self._background_tasks: set[asyncio.Task] = set()

        if hasattr(self.memory_storage, "bind_message_storage"):
            self.memory_storage.bind_message_storage(message_storage)
        elif getattr(self.memory_storage, "message_storage", None) is None:
            self.memory_storage.message_storage = message_storage

    async def retrieve_memories(
        self,
        memory_key: str,
        query: str,
        limit: int = 5,
        journal_date: str | None = None,
    ) -> list:
        """Retrieve recent context plus keyword/date search results."""
        if journal_date is not None:
            return await self.search_memories(
                memory_key=memory_key,
                query=query,
                limit=limit,
                journal_date=journal_date,
            )

        recent_results = await self.retrieve_context_memories(memory_key=memory_key)
        search_results = await self.search_memories(
            memory_key=memory_key,
            query=query,
            limit=limit,
            journal_date=journal_date,
        )
        results = self._merge_memory_results(recent_results, search_results)
        self.logger.debug(
            "Memory retrieval completed: memory_key=%s recent_results=%d search_results=%d merged_results=%d",
            memory_key,
            len(recent_results),
            len(search_results),
            len(results),
        )
        return results

    async def retrieve_context_memories(
        self,
        memory_key: str,
    ) -> list:
        """Retrieve lightweight always-on memory context for the current turn."""
        results = await self._retrieve_recent_day_memories(memory_key=memory_key)
        self.logger.debug(
            "Context memory retrieval completed: memory_key=%s results=%d",
            memory_key,
            len(results),
        )
        return results

    async def search_memories(
        self,
        memory_key: str,
        query: str,
        limit: int = 5,
        journal_date: str | None = None,
    ) -> list:
        """Retrieve memory by explicit keyword search or date filter."""
        self.logger.debug(
            "Memory search dispatch: memory_key=%s query=%r limit=%d journal_date=%s",
            memory_key,
            query,
            limit,
            journal_date,
        )
        if journal_date is not None:
            results = await self.memory_storage.retrieve(
                memory_key=memory_key,
                query=query,
                limit=limit,
                journal_date=journal_date,
            )
            self.logger.debug(
                "Memory search completed with explicit journal_date: memory_key=%s results=%d",
                memory_key,
                len(results),
            )
            return results

        results = await self.memory_storage.retrieve(
            memory_key=memory_key,
            query=query,
            limit=limit,
            journal_date=None,
        )
        self.logger.debug(
            "Memory search completed: memory_key=%s results=%d",
            memory_key,
            len(results),
        )
        return results

    def schedule_memory_add(
        self,
        memory_key: str,
        messages: List[dict],
    ) -> None:
        if not messages:
            self.logger.debug("Skipping background journal add for %s: empty message list", memory_key)
            return

        self.logger.debug(
            "Scheduling background journal add: memory_key=%s messages=%d active_tasks=%d",
            memory_key,
            len(messages),
            len(self._background_tasks),
        )

        task = asyncio.create_task(
            self.memory_storage.add(
                memory_key=memory_key,
                messages=messages,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(lambda done_task: self._on_background_task_done(memory_key, done_task))

    def _on_background_task_done(self, memory_key: str, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            self.logger.warning("Background journal add task cancelled: memory_key=%s", memory_key)
            return

        if exc is not None:
            self.logger.error("Background journal add task failed for %s: %s", memory_key, exc)
            return

        self.logger.debug(
            "Background journal add task finished: memory_key=%s remaining_tasks=%d",
            memory_key,
            len(self._background_tasks),
        )

    async def _retrieve_recent_day_memories(self, memory_key: str) -> list:
        today = datetime.now().date()
        recent_dates = [
            (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(self.ALWAYS_INCLUDE_RECENT_DAYS)
        ]
        self.logger.debug(
            "Retrieving always-included recent day journals: memory_key=%s dates=%s",
            memory_key,
            recent_dates,
        )

        results: list = []
        for date_text in recent_dates:
            day_results = await self.memory_storage.retrieve(
                memory_key=memory_key,
                query="",
                limit=1,
                journal_date=date_text,
            )
            if day_results:
                self.logger.debug(
                    "Recent day journal found: memory_key=%s journal_date=%s results=%d",
                    memory_key,
                    date_text,
                    len(day_results),
                )
                results.extend(day_results)
            else:
                self.logger.debug(
                    "Recent day journal missing: memory_key=%s journal_date=%s",
                    memory_key,
                    date_text,
                )
        return results

    def _merge_memory_results(self, recent_results: list, search_results: list) -> list:
        merged: list = []
        seen_keys: set[tuple[str | None, str | None]] = set()

        for item in [*recent_results, *search_results]:
            item_id = item.get("id") if isinstance(item, dict) else None
            metadata = item.get("metadata", {}) if isinstance(item, dict) else {}
            journal_date = metadata.get("journal_date") if isinstance(metadata, dict) else None
            key = (str(item_id) if item_id is not None else None, str(journal_date) if journal_date is not None else None)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            merged.append(item)
        return merged
