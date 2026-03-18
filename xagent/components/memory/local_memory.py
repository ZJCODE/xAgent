from typing import Optional
import logging

from .basic_memory import MemoryStorageBasic
from .config.memory_config import MEMORY_EXTRACTION_INTERVAL_SECONDS, MEMORY_MAX_BATCH_MESSAGES
from .vector.local_vector_store import VectorStoreLocal


class MemoryStorageLocal(MemoryStorageBasic):
    """
    Local memory storage using ChromaDB with batched LLM-based memory extraction.

    Args:
        path: Path to ChromaDB storage directory. Defaults to ~/.xagent/chroma
        collection_name: Name of the ChromaDB collection. Defaults to 'xagent_memory'
        memory_threshold: Number of unread transcript messages required for periodic extraction
        message_storage: Optional MessageStorage instance for reading message history
        vector_store: Optional VectorStore instance (defaults to VectorStoreLocal)
    """

    def __init__(self,
                 path: str = None,
                 collection_name: str = "xagent_memory",
                 memory_threshold: int = 10,
                 memory_interval_seconds: int = MEMORY_EXTRACTION_INTERVAL_SECONDS,
                 max_batch_messages: int = MEMORY_MAX_BATCH_MESSAGES,
                 message_storage=None,
                 vector_store=None):

        self.logger = logging.getLogger(f"{self.__class__.__name__}")

        # Initialize vector store before calling super().__init__
        self.vector_store = self._initialize_vector_store(
            path=path,
            collection_name=collection_name,
            vector_store=vector_store,
        )

        super().__init__(
            memory_threshold=memory_threshold,
            message_storage=message_storage,
            vector_store=self.vector_store,
            memory_interval_seconds=memory_interval_seconds,
            max_batch_messages=max_batch_messages,
        )

        self.logger.info("LocalMemory initialized with vector store: %s", type(self.vector_store).__name__)

    def _initialize_vector_store(self, path: str = None, collection_name: str = "xagent_memory",
                                 vector_store=None):
        """Initialize the vector store for local memory."""
        if vector_store is None:
            vector_store = VectorStoreLocal(
                path=path,
                collection_name=collection_name,
                embedding_model="text-embedding-3-small",
            )
            self.logger.info("Using default VectorStoreLocal")
        else:
            self.logger.info("Using provided vector store: %s", type(vector_store).__name__)
        return vector_store
