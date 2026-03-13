import asyncio
import logging
from typing import Any, Awaitable, Callable, List, Optional

from .config import AgentConfig
from ..components import MemoryStorageBase, MessageStorageBase


logger = logging.getLogger(__name__)


class MemoryManager:
    """Manages memory retrieval, preprocessing decisions, and background memory writes."""

    def __init__(
        self,
        memory_storage: MemoryStorageBase,
        message_storage: MessageStorageBase,
    ):
        self.memory_storage = memory_storage
        self._background_tasks: set[asyncio.Task] = set()
        self._background_task_semaphore = asyncio.Semaphore(
            AgentConfig.DEFAULT_MAX_BACKGROUND_TASKS
        )

        # Inject message_storage into memory_storage so it can read
        # conversation history directly for memory extraction.
        if self.memory_storage.message_storage is None:
            self.memory_storage.message_storage = message_storage

    async def retrieve_memories(
        self,
        user_id: str,
        query: str,
        pre_chat: Optional[List[dict]] = None,
        limit: int = 5,
    ) -> list:
        """Retrieve relevant memories for the given user and query."""
        query_process = self._should_preprocess_query(query, pre_chat)
        return await self.memory_storage.retrieve(
            user_id=user_id,
            query=query,
            limit=limit,
            query_context=f"pre_chat:{pre_chat}",
            enable_query_process=query_process,
        )

    def schedule_memory_add(
        self,
        user_id: str,
        session_id: str,
        messages: List[dict],
        description: str,
    ) -> None:
        """Schedule memory writes on the controlled background runner."""
        if not messages:
            return
        self._schedule_background_task(
            lambda: self.memory_storage.add(
                user_id=user_id, session_id=session_id, messages=messages
            ),
            description=description,
        )

    def _should_preprocess_query(
        self,
        query: str,
        pre_chat: Optional[List[dict]] = None,
    ) -> bool:
        """Use heuristics by default to avoid unnecessary LLM query rewriting."""
        llm_service = getattr(self.memory_storage, "llm_service", None)
        if llm_service and hasattr(llm_service, "should_preprocess_query"):
            return llm_service.should_preprocess_query(query, pre_chat)
        return False

    def _schedule_background_task(
        self,
        task_factory: Callable[[], Awaitable[Any]],
        description: str,
    ) -> None:
        task = asyncio.create_task(self._run_background_task(task_factory, description))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _run_background_task(
        self,
        task_factory: Callable[[], Awaitable[Any]],
        description: str,
    ) -> None:
        async with self._background_task_semaphore:
            last_error: Optional[Exception] = None
            for attempt in range(1, AgentConfig.BACKGROUND_TASK_ATTEMPTS + 1):
                try:
                    await task_factory()
                    return
                except Exception as exc:
                    last_error = exc
                    logger.warning(
                        "Background task failed (%s), attempt %d/%d: %s",
                        description,
                        attempt,
                        AgentConfig.BACKGROUND_TASK_ATTEMPTS,
                        exc,
                    )
                    if attempt < AgentConfig.BACKGROUND_TASK_ATTEMPTS:
                        await asyncio.sleep(
                            AgentConfig.BACKGROUND_TASK_BASE_DELAY * attempt
                        )

            if last_error is not None:
                logger.error(
                    "Background task permanently failed (%s): %s",
                    description,
                    last_error,
                )
