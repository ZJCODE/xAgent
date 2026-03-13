from typing import Optional
import logging

from .basic_memory import MemoryStorageBasic
from .base_vector_store import VectorStoreBase
from .local_vector_store import VectorStoreLocal


class MemoryStorageLocal(MemoryStorageBasic):
    """
    Local memory storage using ChromaDB with LLM-based memory extraction.

    Args:
        path: Path to ChromaDB storage directory. Defaults to ~/.xagent/chroma
        collection_name: Name of the ChromaDB collection. Defaults to 'xagent_memory'
        memory_threshold: Number of messages to trigger long-term storage. Defaults to 10
        message_storage: Optional MessageStorage instance for reading conversation history
        vector_store: Optional VectorStore instance (defaults to VectorStoreLocal)
    """

    def __init__(self,
                 path: str = None,
                 collection_name: str = "xagent_memory",
                 memory_threshold: int = 10,
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
