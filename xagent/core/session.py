# Standard library imports
import logging
from typing import Dict, List, Optional, Tuple, Union

# Local imports
from ..db import MessageDB
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
    across different storage backends (local memory or database). It supports:
    - Adding messages to session history
    - Retrieving recent messages with count limits
    - Clearing session history
    - Popping messages from history
    - Automatic history size management for local storage
    
    Attributes:
        user_id: Unique identifier for the user
        session_id: Unique identifier for the session
        message_db: Optional database backend for persistent storage
        logger: Logger instance for this session
    
    Class Attributes:
        _local_messages: Class-level storage for local message history
                        Format: {(user_id, session_id): [Message, ...]}
    """
    
    # Class-level storage for local message history
    _local_messages: Dict[Tuple[str, str], List[Message]] = {}

    def __init__(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message_db: Optional[MessageDB] = None
    ):
        """
        Initialize a Session instance.
        
        Args:
            user_id: Unique identifier for the user. Defaults to "default_user"
            session_id: Unique identifier for the session. Defaults to "default_session"
            message_db: Optional database backend. If None, uses local memory storage
            
        Note:
            When using local storage, session data persists only during the
            application lifetime and is shared across all Session instances.
        """
        self.user_id = user_id or SessionConfig.DEFAULT_USER_ID
        self.session_id = session_id or SessionConfig.DEFAULT_SESSION_ID
        self.message_db = message_db
        
        # Initialize local storage if using memory backend
        if not self.message_db:
            self._ensure_local_session_exists()
        
        # Create logger with session context
        self.logger = logging.getLogger(
            f"{self.__class__.__name__}[{self.user_id}:{self.session_id}]"
        )
    
    def _ensure_local_session_exists(self) -> None:
        """Ensure local session storage exists for this session."""
        session_key = self._get_session_key()
        if session_key not in Session._local_messages:
            Session._local_messages[session_key] = []
    
    def _get_session_key(self) -> Tuple[str, str]:
        """Get the session key for local storage."""
        return (self.user_id, self.session_id)

    async def add_messages(self, messages: Union[Message, List[Message]]) -> None:
        """
        Add messages to the session history.
        
        Args:
            messages: A single Message object or a list of Message objects to add
            
        Raises:
            Exception: If database operation fails (logged but not re-raised)
            
        Note:
            For local storage, automatically trims history to MAX_LOCAL_HISTORY
            to prevent memory issues.
        """
        normalized_messages = self._normalize_messages_input(messages)
        
        try:
            if self.message_db:
                await self._add_messages_to_db(normalized_messages)
            else:
                await self._add_messages_to_local(normalized_messages)
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
    
    async def _add_messages_to_db(self, messages: List[Message]) -> None:
        """Add messages to database backend."""
        self.logger.info("Adding %d messages to DB", len(messages))
        await self.message_db.add_messages(self.user_id, messages, self.session_id)
    
    async def _add_messages_to_local(self, messages: List[Message]) -> None:
        """Add messages to local memory storage with history management."""
        session_key = self._get_session_key()
        self.logger.info("Adding %d messages to local session", len(messages))
        
        # Add messages and manage history size
        Session._local_messages[session_key].extend(messages)
        self._trim_local_history(session_key)
    
    def _trim_local_history(self, session_key: Tuple[str, str]) -> None:
        """Trim local history to maximum allowed size."""
        messages = Session._local_messages[session_key]
        if len(messages) > SessionConfig.MAX_LOCAL_HISTORY:
            Session._local_messages[session_key] = messages[-SessionConfig.MAX_LOCAL_HISTORY:]

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
            if self.message_db:
                return await self._get_messages_from_db(count)
            else:
                return await self._get_messages_from_local(count)
        except Exception as e:
            self.logger.error("Failed to get messages: %s", e)
            return []
    
    async def _get_messages_from_db(self, count: int) -> List[Message]:
        """Get messages from database backend."""
        self.logger.info("Fetching last %d messages from DB", count)
        return await self.message_db.get_messages(self.user_id, self.session_id, count)
    
    async def _get_messages_from_local(self, count: int) -> List[Message]:
        """Get messages from local memory storage."""
        session_key = self._get_session_key()
        self.logger.info("Fetching last %d messages from local session", count)
        messages = Session._local_messages[session_key]
        return messages[-count:] if messages else []

    async def clear_session(self) -> None:
        """
        Clear the session history.
        
        This will remove all messages from the current session.
        For database backend, calls the database clear method.
        For local storage, clears the in-memory message list.
        
        Raises:
            Exception: If database operation fails (logged but not re-raised)
        """
        try:
            if self.message_db:
                await self._clear_db_session()
            else:
                await self._clear_local_session()
        except Exception as e:
            self.logger.error("Failed to clear session: %s", e)
    
    async def _clear_db_session(self) -> None:
        """Clear session history in database."""
        self.logger.info("Clearing history in DB")
        await self.message_db.clear_history(self.user_id, self.session_id)
    
    async def _clear_local_session(self) -> None:
        """Clear session history in local memory."""
        session_key = self._get_session_key()
        self.logger.info("Clearing local session history")
        Session._local_messages[session_key] = []

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
            For local storage, this modifies the in-memory message list.
            For database storage, this calls the database pop method.
        """
        try:
            if self.message_db:
                return await self._pop_message_from_db()
            else:
                return await self._pop_message_from_local()
        except Exception as e:
            self.logger.error("Failed to pop message: %s", e)
            return None
    
    async def _pop_message_from_db(self) -> Optional[Message]:
        """Pop message from database backend."""
        self.logger.info("Popping last message from DB")
        return await self.message_db.pop_message(self.user_id, self.session_id)
    
    async def _pop_message_from_local(self) -> Optional[Message]:
        """Pop message from local memory storage, skipping tool messages."""
        session_key = self._get_session_key()
        self.logger.info("Popping last message from local session")
        
        messages = Session._local_messages[session_key]
        while messages:
            msg = messages.pop()
            if not self._is_tool_message(msg):
                return msg
        return None
    
    def _is_tool_message(self, message: Message) -> bool:
        """Check if a message is a tool-related message."""
        return bool(getattr(message, 'tool_call', None))
    
    async def get_message_count(self) -> int:
        """
        Get the total number of messages in the session.
        
        Returns:
            Total number of messages in the session history
        """
        try:
            if self.message_db:
                # Assuming MessageDB has a count method, otherwise get all and count
                messages = await self.message_db.get_messages(
                    self.user_id, self.session_id, float('inf')
                )
                return len(messages)
            else:
                session_key = self._get_session_key()
                return len(Session._local_messages[session_key])
        except Exception as e:
            self.logger.error("Failed to get message count: %s", e)
            return 0
    
    async def has_messages(self) -> bool:
        """
        Check if the session has any messages.
        
        Returns:
            True if session contains messages, False otherwise
        """
        return await self.get_message_count() > 0
    
    def get_session_info(self) -> Dict[str, str]:
        """
        Get session information.
        
        Returns:
            Dictionary containing session metadata
        """
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "backend": "database" if self.message_db else "local",
            "session_key": f"{self.user_id}:{self.session_id}"
        }
    
    @classmethod
    def get_all_local_sessions(cls) -> List[Tuple[str, str]]:
        """
        Get all local session keys.
        
        Returns:
            List of (user_id, session_id) tuples for all local sessions
        """
        return list(cls._local_messages.keys())
    
    @classmethod
    def clear_all_local_sessions(cls) -> None:
        """Clear all local session data."""
        cls._local_messages.clear()
    
    def __str__(self) -> str:
        """String representation of the session."""
        return f"Session(user_id='{self.user_id}', session_id='{self.session_id}')"
    
    def __repr__(self) -> str:
        """Detailed string representation of the session."""
        backend = "database" if self.message_db else "local"
        return f"Session(user_id='{self.user_id}', session_id='{self.session_id}', backend='{backend}')"
