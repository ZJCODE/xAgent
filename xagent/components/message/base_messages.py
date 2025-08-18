# Standard library imports
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Union

# Local imports
from ...schemas import Message


class MessageStorageBase(ABC):
    """
    Abstract base class for message storage backends.
    
    This class defines the common interface that all message storage backends
    must implement. It ensures consistency between different storage implementations
    such as LocalDB (in-memory) and MessageDB (Redis-based).
    
    All methods that interact with storage are declared as abstract and must
    be implemented by concrete subclasses.
    """

    @abstractmethod
    async def add_messages(
        self,
        user_id: str,
        session_id: str,
        messages: Union[Message, List[Message]],
        **kwargs
    ) -> None:
        """
        Add messages to the session history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            messages: Single Message object or list of Message objects
            **kwargs: Additional backend-specific arguments
            
        Raises:
            ValueError: If parameters are invalid
            Exception: If storage operation fails
        """
        pass

    @abstractmethod
    async def get_messages(
        self, 
        user_id: str, 
        session_id: str, 
        count: int = 20
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
            Exception: If storage operation fails
        """
        pass

    @abstractmethod
    async def clear_history(self, user_id: str, session_id: str) -> None:
        """
        Clear the session history.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Raises:
            Exception: If storage operation fails
        """
        pass

    @abstractmethod
    async def pop_message(self, user_id: str, session_id: str) -> Optional[Message]:
        """
        Pop the last message from the session history.
        
        This method removes and returns the last message from the session.
        Implementations should handle tool messages according to their logic.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            The last message, or None if no message exists or session is empty
            
        Raises:
            Exception: If storage operation fails
        """
        pass

    # Optional methods with default implementations
    async def get_message_count(self, user_id: str, session_id: str) -> int:
        """
        Get the total number of messages in the session.
        
        Default implementation uses get_messages with a large count.
        Subclasses can override for more efficient implementations.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Total number of messages in the session history
        """
        try:
            # Use a large number to get all messages, then count
            messages = await self.get_messages(user_id, session_id, 999999)
            return len(messages)
        except Exception:
            return 0

    async def has_messages(self, user_id: str, session_id: str) -> bool:
        """
        Check if the session has any messages.
        
        Default implementation uses get_message_count.
        Subclasses can override for more efficient implementations.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            True if session contains messages, False otherwise
        """
        return await self.get_message_count(user_id, session_id) > 0

    def get_session_info(self, user_id: str, session_id: str) -> Dict[str, str]:
        """
        Get session information.
        
        Default implementation provides basic info.
        Subclasses should override to provide backend-specific information.
        
        Args:
            user_id: User identifier
            session_id: Session identifier
            
        Returns:
            Dictionary containing session metadata
        """
        return {
            "user_id": user_id,
            "session_id": session_id,
            "backend": self.__class__.__name__.lower().replace("db", ""),
            "session_key": f"{user_id}:{session_id}"
        }



    def __str__(self) -> str:
        """String representation of the storage backend."""
        return f"{self.__class__.__name__}()"

    def __repr__(self) -> str:
        """Detailed string representation of the storage backend."""
        return f"{self.__class__.__name__}()"
