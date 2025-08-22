# Standard library imports
import logging
from typing import Dict, List, Optional, Tuple, Union

# Local imports
from .base_messages import MessageStorageBase
from ...schemas import Message,MessageType


class MessageStorageLocalConfig:
    """Configuration constants for MessageStorageLocal class."""
    
    DEFAULT_MESSAGE_COUNT = 20
    MAX_LOCAL_HISTORY = 100


class MessageStorageLocal(MessageStorageBase):
    """
    Local memory-based message storage backend for conversation history.
    
    This class provides a simple in-memory storage solution for conversation
    messages, compatible with the MessageDB interface. It supports:
    
    Features:
    - Multi-user and multi-session isolation
    - Automatic history trimming to manage memory usage
    - Session-based message storage
    - Compatible interface with MessageDB
    
    Storage Format:
    - Keys: (user_id, session_id) tuples
    - Values: Lists of Message objects
    
    Attributes:
        _messages: Dictionary storing messages for each session
        logger: Logger instance for this class
    
    Note:
        This storage is volatile and will be lost when the application restarts.
        It's intended for testing, development, or scenarios where persistence
        is not required.
    """

    def __init__(self):
        """
        Initialize LocalDB instance.
        """
        self._messages: Dict[Tuple[str, str], List[Message]] = {}
        self.logger = logging.getLogger(f"{self.__class__.__name__}")

    def _get_session_key(self, user_id: str, session_id: str) -> Tuple[str, str]:
        """
        Get the session key for local storage.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Tuple of (user_id, session_id)
        """
        return (user_id, session_id)

    def _ensure_session_exists(self, session_key: Tuple[str, str]) -> None:
        """Ensure session storage exists for the given session key."""
        if session_key not in self._messages:
            self._messages[session_key] = []

    def _trim_history(self, session_key: Tuple[str, str]) -> None:
        """Trim local history to maximum allowed size."""
        messages = self._messages[session_key]
        if len(messages) > MessageStorageLocalConfig.MAX_LOCAL_HISTORY:
            self._messages[session_key] = messages[-MessageStorageLocalConfig.MAX_LOCAL_HISTORY:]
            self.logger.debug(
                "Trimmed session %s history to %d messages", 
                session_key, MessageStorageLocalConfig.MAX_LOCAL_HISTORY
            )

    async def add_messages(
        self,
        user_id: str,
        session_id: str,
        messages: Union[Message, List[Message]],
        **kwargs  # Accept additional kwargs for compatibility with MessageDB
    ) -> None:
        """
        Add messages to the session history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            messages: Single Message object or list of Message objects
            **kwargs: Additional arguments (ignored, for compatibility)
            
        Note:
            Automatically trims history to MAX_LOCAL_HISTORY to prevent memory issues.
        """
        # Normalize input to list
        if not isinstance(messages, list):
            messages = [messages]
        
        session_key = self._get_session_key(user_id, session_id)
        self._ensure_session_exists(session_key)
        
        self.logger.info("Adding %d messages to local session %s", len(messages), session_key)
        
        # Add messages and manage history size
        self._messages[session_key].extend(messages)
        self._trim_history(session_key)

    async def get_messages(
        self, 
        user_id: str, 
        session_id: str, 
        count: int = MessageStorageLocalConfig.DEFAULT_MESSAGE_COUNT
    ) -> List[Message]:
        """
        Get the last `count` messages from the session history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            count: Number of messages to retrieve. Must be positive.
            
        Returns:
            List of Message objects from the session history, ordered chronologically.
            Returns empty list if no messages exist.
            
        Raises:
            ValueError: If count is not positive
        """
        if count <= 0:
            raise ValueError("count must be a positive integer")
        
        session_key = self._get_session_key(user_id, session_id)
        self._ensure_session_exists(session_key)
        
        messages = self._messages[session_key]
        result_messages = messages[-count:] if messages else []
        
        self.logger.debug(
            "Retrieved %d/%d messages for session %s", 
            len(result_messages), len(messages), session_key
        )
        return result_messages

    async def clear_history(self, user_id: str, session_id: str) -> None:
        """
        Clear the session history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
        """
        session_key = self._get_session_key(user_id, session_id)
        self._ensure_session_exists(session_key)
        
        message_count = len(self._messages[session_key])
        self.logger.info("Clearing local session history for %s", session_key)
        self._messages[session_key] = []
        self.logger.debug("Cleared history for session %s (deleted: %d)", session_key, message_count)

    async def pop_message(self, user_id: str, session_id: str) -> Optional[Message]:
        """
        Pop the last message from the session history.
        
        This method removes and returns the last message from the session.
        If the last message is a tool result, it will continue popping until 
        a non-tool result message is found.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            The last non-tool result message, or None if no such message exists
            or if the session is empty.
        """
        session_key = self._get_session_key(user_id, session_id)
        self._ensure_session_exists(session_key)
        
        self.logger.info("Popping last message from local session %s", session_key)
        
        messages = self._messages[session_key]
        messages_popped = 0
        while messages:
            msg = messages.pop()
            messages_popped += 1
            if not self._is_tool_message(msg):
                self.logger.debug(
                    "Popped non-tool message for session %s (checked %d messages)", 
                    session_key, messages_popped
                )
                return msg
            
            # Continue if this is a tool message
            self.logger.debug("Skipping tool message for session %s", session_key)
        
        self.logger.debug("No messages found for session %s", session_key)
        return None

    def _is_tool_message(self, message: Message) -> bool:
        """Check if a message is a tool-related message."""
        return message.type in {MessageType.FUNCTION_CALL, MessageType.FUNCTION_CALL_OUTPUT}



    async def get_message_count(self, user_id: str, session_id: str) -> int:
        """
        Get the total number of messages in the session.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Total number of messages in the session history
        """
        session_key = self._get_session_key(user_id, session_id)
        self._ensure_session_exists(session_key)
        return len(self._messages[session_key])

    async def has_messages(self, user_id: str, session_id: str) -> bool:
        """
        Check if the session has any messages.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            True if session contains messages, False otherwise
        """
        return await self.get_message_count(user_id, session_id) > 0

    def get_all_sessions(self) -> List[Tuple[str, str]]:
        """
        Get all session keys.
        
        Returns:
            List of (user_id, session_id) tuples for all sessions
        """
        return list(self._messages.keys())

    def clear_all_sessions(self) -> None:
        """Clear all session data."""
        self.logger.info("Clearing all local sessions")
        self._messages.clear()

    def get_session_info(self, user_id: str, session_id: str) -> Dict[str, str]:
        """
        Get session information.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Dictionary containing session metadata
        """
        session_key = self._get_session_key(user_id, session_id)
        return {
            "user_id": user_id,
            "session_id": session_id,
            "backend": "local",
            "session_key": f"{session_key[0]}:{session_key[1]}",
            "message_count": str(len(self._messages.get(session_key, [])))
        }

    def __str__(self) -> str:
        """String representation of the LocalDB."""
        return f"LocalDB(sessions={len(self._messages)})"

    def __repr__(self) -> str:
        """Detailed string representation of the LocalDB."""
        return f"LocalDB(sessions={len(self._messages)}, total_messages={sum(len(msgs) for msgs in self._messages.values())})"
