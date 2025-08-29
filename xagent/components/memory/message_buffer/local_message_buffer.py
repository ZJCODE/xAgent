import logging
from typing import List, Dict, Any
from .base_message_buffer import MessageBufferBase


class MessageBufferLocal(MessageBufferBase):
    """
    Local in-memory message buffer implementation.
    
    This class provides in-memory storage for temporary user messages that are used
    by LocalMemory before they are converted to long-term memory storage.
    
    Features:
    - Per-user message lists stored in memory
    - Thread-safe operations
    - Configurable maximum messages per user
    
    Storage Format:
    - Dictionary with user_id as key and list of message dictionaries as value
    """
    
    def __init__(self, max_messages: int = 100):
        """
        Initialize MessageBufferLocal instance.
        
        Args:
            max_messages: Maximum messages to store per user. Defaults to 100
        """
        self._user_messages: Dict[str, List[Dict[str, Any]]] = {}
        self.max_messages = max_messages
        self.logger = logging.getLogger(f"{self.__class__.__name__}")
        
        self.logger.info("MessageBufferLocal initialized with max_messages: %d", max_messages)
    
    async def add_messages(self, user_id: str, messages: List[Dict[str, Any]]) -> None:
        """
        Add messages to user's temporary storage.
        
        Args:
            user_id: User identifier
            messages: List of message dictionaries to add
        """
        if not messages:
            self.logger.debug("No messages provided for user %s", user_id)
            return
        
        # Initialize user's message list if not exists
        if user_id not in self._user_messages:
            self._user_messages[user_id] = []
        
        # Add new messages
        self._user_messages[user_id].extend(messages)
        
        # Trim to max length to prevent memory overflow
        if len(self._user_messages[user_id]) > self.max_messages:
            self._user_messages[user_id] = self._user_messages[user_id][-self.max_messages:]
        
        self.logger.debug("Added %d messages for user %s, total messages: %d", 
                         len(messages), user_id, len(self._user_messages[user_id]))
    
    async def get_messages(self, user_id: str) -> List[Dict[str, Any]]:
        """
        Get all messages for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            List of message dictionaries
        """
        messages = self._user_messages.get(user_id, [])
        self.logger.debug("Retrieved %d messages for user %s", len(messages), user_id)
        return messages.copy()  # Return a copy to prevent external modification
    
    async def get_message_count(self, user_id: str) -> int:
        """
        Get the number of messages for a user.
        
        Args:
            user_id: User identifier
            
        Returns:
            Number of messages
        """
        count = len(self._user_messages.get(user_id, []))
        self.logger.debug("User %s has %d messages", user_id, count)
        return count
    
    async def keep_recent_messages(self, user_id: str, keep_count: int) -> None:
        """
        Keep only the most recent N messages for a user.
        
        Args:
            user_id: User identifier
            keep_count: Number of recent messages to keep
        """
        if keep_count <= 0:
            await self.clear_messages(user_id)
            return
        
        if user_id in self._user_messages:
            original_count = len(self._user_messages[user_id])
            self._user_messages[user_id] = self._user_messages[user_id][-keep_count:]
            new_count = len(self._user_messages[user_id])
            self.logger.debug("Kept %d recent messages for user %s (removed %d)", 
                             new_count, user_id, original_count - new_count)
    
    async def clear_messages(self, user_id: str) -> None:
        """
        Clear all messages for a user.
        
        Args:
            user_id: User identifier
        """
        if user_id in self._user_messages:
            message_count = len(self._user_messages[user_id])
            del self._user_messages[user_id]
            self.logger.debug("Cleared %d messages for user %s", message_count, user_id)
        else:
            self.logger.debug("No messages to clear for user %s", user_id)
    
    def get_user_count(self) -> int:
        """
        Get the number of users with stored messages.
        
        Returns:
            Number of users
        """
        return len(self._user_messages)
    
    def get_total_message_count(self) -> int:
        """
        Get the total number of messages across all users.
        
        Returns:
            Total number of messages
        """
        return sum(len(messages) for messages in self._user_messages.values())
    
    def __repr__(self) -> str:
        """String representation of MessageBufferLocal instance."""
        return f"MessageBufferLocal(users={self.get_user_count()}, total_messages={self.get_total_message_count()}, max_messages={self.max_messages})"
