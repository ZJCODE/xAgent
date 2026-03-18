import logging
import warnings
from typing import Optional

from .basic_memory import MemoryStorageBasic
from .config.memory_config import MEMORY_EXTRACTION_INTERVAL_SECONDS, MEMORY_MAX_BATCH_MESSAGES


class MemoryStorageLocal(MemoryStorageBasic):
    """Local SQLite journal memory stored in the same database as messages."""

    def __init__(
        self,
        path: Optional[str] = None,
        collection_name: str = "xagent_memory",
        memory_threshold: int = 10,
        memory_interval_seconds: int = MEMORY_EXTRACTION_INTERVAL_SECONDS,
        max_batch_messages: int = MEMORY_MAX_BATCH_MESSAGES,
        message_storage=None,
    ):
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        if collection_name != "xagent_memory":
            warnings.warn(
                "MemoryStorageLocal.collection_name is deprecated and ignored; "
                "journal memory now lives in the shared SQLite database.",
                DeprecationWarning,
                stacklevel=2,
            )

        super().__init__(
            path=path,
            memory_threshold=memory_threshold,
            message_storage=message_storage,
            memory_interval_seconds=memory_interval_seconds,
            max_batch_messages=max_batch_messages,
        )
        if path:
            self.logger.info("Local journal memory initialized at %s", self.path)
