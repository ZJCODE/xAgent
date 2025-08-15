# Standard library imports
import logging
from typing import Dict, List, Optional, Union

# Local imports
from ..db import MessageStorageBase,MessageStorageLocal
from ..schemas import Message


class SessionConfig:
    """Configuration constants for Session class."""
    
    DEFAULT_USER_ID = "default_user"
    DEFAULT_SESSION_ID = "default_session"
    DEFAULT_MESSAGE_COUNT = 20
    MAX_LOCAL_HISTORY = 100

class Session:
    """
    Session class to manage user sessions and message history.
    
    This class provides a unified interface for managing conversation history
    using any MessageStorageBase implementation. It supports:
    - Adding messages to session history
    - Retrieving recent messages with count limits
    - Clearing session history
    - Popping messages from history
    - Automatic history size management (depending on message_storage backend)
    
    Attributes:
        user_id: Unique identifier for the user
        session_id: Unique identifier for the session
        message_storage: MessageStorageBase instance for message message_storage
        logger: Logger instance for this session
    """
    
    def __init__(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message_storage: Optional[MessageStorageBase] = None
    ):
        """
        Initialize a Session instance.
        
        Args:
            user_id: Unique identifier for the user. Defaults to "default_user"
            session_id: Unique identifier for the session. Defaults to "default_session"
            message_storage: MessageStorageBase instance (LocalDB, MessageDB, or custom implementation).
                    If None, creates a new LocalDB instance.
            
        """
        self.user_id = user_id or SessionConfig.DEFAULT_USER_ID
        self.session_id = session_id or SessionConfig.DEFAULT_SESSION_ID
        
        # Use provided message_storage or default to LocalDB
        if message_storage is not None:
            self.message_storage = message_storage
        else:
            # Default to LocalDB
            self.message_storage = MessageStorageLocal()
        
        # Create logger with session context
        self.logger = logging.getLogger(
            f"{self.__class__.__name__}[{self.user_id}:{self.session_id}]"
        )

    async def add_messages(self, messages: Union[Message, List[Message]]) -> None:
        """
        Add messages to the session history.
        
        Args:
            messages: A single Message object or a list of Message objects to add
            
        Raises:
            Exception: If database operation fails (logged but not re-raised)
            
        Note:
            For local message_storage, automatically trims history to MAX_LOCAL_HISTORY
            to prevent memory issues.
        """
        normalized_messages = self._normalize_messages_input(messages)
        
        try:
            await self.message_storage.add_messages(self.user_id, normalized_messages, self.session_id)
        except Exception as e:
            self.logger.error("Failed to add messages: %s", e)
    
    def _normalize_messages_input(
        self, 
        messages: Union[Message, List[Message]]
    ) -> List[Message]:
        """Normalize input to a list of messages."""
        if not isinstance(messages, list):
            return [messages]
        return messages
    
    async def get_messages(self, count: int = SessionConfig.DEFAULT_MESSAGE_COUNT) -> List[Message]:
        """
        Get the last `count` messages from the session history.
        
        Args:
            count: Number of messages to retrieve. Must be positive.
                  Defaults to DEFAULT_MESSAGE_COUNT (20)
            
        Returns:
            List of Message objects from the session history, ordered chronologically.
            Returns empty list if no messages exist or on error.
            
        Raises:
            ValueError: If count is not positive
        """
        if count <= 0:
            raise ValueError("Message count must be positive")
        
        try:
            return await self.message_storage.get_messages(self.user_id, self.session_id, count)
        except Exception as e:
            self.logger.error("Failed to get messages: %s", e)
            return []
    
    async def clear_session(self) -> None:
        """
        Clear the session history.
        
        This will remove all messages from the current session.
        For database backend, calls the database clear method.
        For local message_storage, clears the in-memory message list.
        
        Raises:
            Exception: If database operation fails (logged but not re-raised)
        """
        try:
            await self.message_storage.clear_history(self.user_id, self.session_id)
        except Exception as e:
            self.logger.error("Failed to clear session: %s", e)
    
    async def pop_message(self) -> Optional[Message]:
        """
        Pop the last message from the session history.
        
        This method removes and returns the last message from the session.
        If the last message is a tool result, it will continue popping until 
        a non-tool result message is found.
        
        Returns:
            The last non-tool result message, or None if no such message exists
            or if the session is empty.
            
        Note:
            For local message_storage, this modifies the in-memory message list.
            For database message_storage, this calls the database pop method.
        """
        try:
            return await self.message_storage.pop_message(self.user_id, self.session_id)
        except Exception as e:
            self.logger.error("Failed to pop message: %s", e)
            return None
    
    async def get_message_count(self) -> int:
        """
        Get the total number of messages in the session.
        
        Returns:
            Total number of messages in the session history
        """
        try:
            return await self.message_storage.get_message_count(self.user_id, self.session_id)
        except Exception as e:
            self.logger.error("Failed to get message count: %s", e)
            return 0
    
    async def has_messages(self) -> bool:
        """
        Check if the session has any messages.
        
        Returns:
            True if session contains messages, False otherwise
        """
        return await self.message_storage.has_messages(self.user_id, self.session_id)
    
    def get_session_info(self) -> Dict[str, str]:
        """
        Get session information.
        
        Returns:
            Dictionary containing session metadata
        """
        return self.message_storage.get_session_info(self.user_id, self.session_id)
    
    
    def __str__(self) -> str:
        """String representation of the session."""
        return f"Session(user_id='{self.user_id}', session_id='{self.session_id}')"
    
    def __repr__(self) -> str:
        """Detailed string representation of the session."""
        message_storage_type = type(self.message_storage).__name__
        return f"Session(user_id='{self.user_id}', session_id='{self.session_id}', message_storage='{message_storage_type}')"
