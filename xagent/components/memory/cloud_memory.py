from typing import Optional
import logging

from .basic_memory import MemoryStorageBasic
from .config.memory_config import MEMORY_EXTRACTION_INTERVAL_SECONDS, MEMORY_MAX_BATCH_MESSAGES
from .vector.cloud_vector_store import VectorStoreUpstash


class MemoryStorageCloud(MemoryStorageBasic):
    """
    Cloud memory storage backed by Upstash Vector with batched LLM-based memory extraction.

    Args:
        memory_threshold: Number of unread transcript messages required for periodic extraction
        message_storage: Optional MessageStorage instance for reading message history
        vector_store: Optional VectorStore instance (defaults to VectorStoreUpstash)
    """

    def __init__(self,
                 memory_threshold: int = 10,
                 memory_interval_seconds: int = MEMORY_EXTRACTION_INTERVAL_SECONDS,
                 max_batch_messages: int = MEMORY_MAX_BATCH_MESSAGES,
                 message_storage=None,
                 vector_store=None):

        self.logger = logging.getLogger(f"{self.__class__.__name__}")

        # Initialize vector store before calling super().__init__
        self.vector_store = self._initialize_vector_store(vector_store=vector_store)

        super().__init__(
            memory_threshold=memory_threshold,
            message_storage=message_storage,
            vector_store=self.vector_store,
            memory_interval_seconds=memory_interval_seconds,
            max_batch_messages=max_batch_messages,
        )

        self.logger.info(
            "MemoryStorageCloud initialized with threshold: %d",
            memory_threshold,
        )

    def _initialize_vector_store(self, vector_store=None):
        """Initialize the vector store for cloud memory."""
        if vector_store is None:
            vector_store = VectorStoreUpstash()
            self.logger.info("Using default VectorStoreUpstash")
        else:
            self.logger.info("Using provided vector store: %s", type(vector_store).__name__)
        return vector_store
