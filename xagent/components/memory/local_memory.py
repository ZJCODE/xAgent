from typing import Optional
import logging
import dotenv

from .basic_memory import MemoryStorageBasic
from .message_buffer import MessageBufferBase, MessageBufferLocal
from .vector_store import VectorStoreBase, VectorStoreLocal

dotenv.load_dotenv(override=True)

class MemoryStorageLocal(MemoryStorageBasic):
    """
    Local memory storage using ChromaDB with LLM-based memory extraction.
    
    Args:
        path: Path to ChromaDB storage directory. Defaults to ~/.xagent/chroma
        collection_name: Name of the ChromaDB collection. Defaults to 'xagent_memory'
        memory_threshold: Number of messages to trigger long-term storage. Defaults to 10
        keep_recent: Number of recent messages to keep after storage. Defaults to 2
    """
    
    def __init__(self, 
                 path: str = None,
                 collection_name: str = "xagent_memory",
                 memory_threshold: int = 10,
                 keep_recent: int = 2,
                 message_buffer: Optional[MessageBufferBase] = None,
                 vector_store: Optional[VectorStoreBase] = None):
        
        # Initialize logger first
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize vector store and message buffer before calling super().__init__
        self.vector_store = self._initialize_vector_store(
            path=path, 
            collection_name=collection_name, 
            vector_store=vector_store
        )
        self.message_buffer = self._initialize_message_buffer(message_buffer=message_buffer)
        
        # Call parent constructor
        super().__init__(
            memory_threshold=memory_threshold,
            keep_recent=keep_recent,
            message_buffer=self.message_buffer,
            vector_store=self.vector_store
        )
        
        self.logger.info("LocalMemory initialized with vector store: %s", type(self.vector_store).__name__)
    
    def _initialize_vector_store(self, path: str = None, collection_name: str = "xagent_memory", 
                                vector_store: Optional[VectorStoreBase] = None) -> VectorStoreBase:
        """Initialize the vector store for local memory."""
        if vector_store is None:
            # Use default VectorStoreLocal with the provided path and collection_name
            vector_store = VectorStoreLocal(
                path=path,
                collection_name=collection_name,
                embedding_model="text-embedding-3-small"
            )
            self.logger.info("Using default VectorStoreLocal")
        else:
            self.logger.info("Using provided vector store: %s", type(vector_store).__name__)
        
        return vector_store
    
    def _initialize_message_buffer(self, message_buffer: Optional[MessageBufferBase] = None) -> MessageBufferBase:
        """Initialize the message buffer for local memory."""
        if message_buffer is None:
            message_buffer = MessageBufferLocal(max_messages=100)
            self.logger.info("Using default MessageBufferLocal")
        else:
            self.logger.info("Using provided message buffer: %s", type(message_buffer).__name__)
        
        return message_buffer
