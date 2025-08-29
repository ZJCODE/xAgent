from typing import Optional
import logging
import dotenv

from .basic_memory import MemoryStorageBasic
from .message_buffer import MessageBufferBase, MessageBufferRedis
from .vector_store import VectorStoreBase, VectorStoreUpstash

dotenv.load_dotenv(override=True)

class MemoryStorageUpstash(MemoryStorageBasic):
    """
    Upstash Vector memory storage with LLM-based memory extraction.
    
    Args:
        memory_threshold: Number of messages to trigger long-term storage. Defaults to 10
        keep_recent: Number of recent messages to keep after storage. Defaults to 2
    """
    
    def __init__(self, 
                 memory_threshold: int = 10,
                 keep_recent: int = 2,
                 message_buffer: Optional[MessageBufferBase] = None,
                 vector_store: Optional[VectorStoreBase] = None):
        
        # Initialize logger first
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        # Initialize vector store and message buffer before calling super().__init__
        self.vector_store = self._initialize_vector_store(vector_store=vector_store)
        self.message_buffer = self._initialize_message_buffer(message_buffer=message_buffer)
        
        # Call parent constructor
        super().__init__(
            memory_threshold=memory_threshold,
            keep_recent=keep_recent,
            message_buffer=self.message_buffer,
            vector_store=self.vector_store
        )
        
        self.logger.info("UpstashMemory initialized with threshold: %d, keep_recent: %d", 
                        memory_threshold, keep_recent)
    
    def _initialize_vector_store(self, vector_store: Optional[VectorStoreBase] = None) -> VectorStoreBase:
        """Initialize the vector store for Upstash memory."""
        if vector_store is None:
            vector_store = VectorStoreUpstash()
            self.logger.info("Using default VectorStoreUpstash")
        else:
            self.logger.info("Using provided vector store: %s", type(vector_store).__name__)
        
        return vector_store
    
    def _initialize_message_buffer(self, message_buffer: Optional[MessageBufferBase] = None) -> MessageBufferBase:
        """Initialize the message buffer for Upstash memory."""
        if message_buffer is None:
            message_buffer = MessageBufferRedis(max_messages=100)
            self.logger.info("Using default MessageBufferRedis")
        else:
            self.logger.info("Using provided message buffer: %s", type(message_buffer).__name__)
        
        return message_buffer

    async def close(self) -> None:
        """Close message buffer connection and cleanup resources."""
        try:
            # Close the message buffer if it has a close method
            if hasattr(self.message_buffer, 'close'):
                await self.message_buffer.close()
                self.logger.info("Message buffer resources cleaned up successfully")
        except Exception as e:
            self.logger.error("Error closing message buffer resources: %s", str(e))
