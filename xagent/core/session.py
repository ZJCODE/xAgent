import logging
from typing import List, Optional

from ..schemas import Message
from ..db import MessageDB

class Session:
    """
    Session class to manage user sessions and message history.
    """
    _local_messages = {}  # {(user_id, session_id): [Message, ...]}

    def __init__(
        self,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
        message_db: Optional[MessageDB] = None
    ):
        self.user_id = user_id or "default_user"
        self.session_id = session_id or "default_session"
        self.message_db = message_db
        key = (self.user_id, self.session_id)
        if not self.message_db and key not in Session._local_messages:
            Session._local_messages[key] = []
        self.logger = logging.getLogger(f"{self.__class__.__name__}[{self.user_id}:{self.session_id}]")

    async def add_messages(self, messages: Message | List[Message]) -> None:
        """
        Add messages to the session history.
        Args:
            messages: A single Message object or a list of Message objects to add.
        """
        try:
            if not isinstance(messages, list):
                messages = [messages]
            if self.message_db:
                self.logger.info("Adding messages to DB: %s", messages)
                await self.message_db.add_messages(self.user_id, messages, self.session_id)
            else:
                key = (self.user_id, self.session_id)
                self.logger.info("Adding messages to local session: %s", messages)
                max_local_history = 100
                Session._local_messages[key].extend(messages)
                if len(Session._local_messages[key]) > max_local_history:
                    Session._local_messages[key] = Session._local_messages[key][-max_local_history:]
        except Exception as e:
            self.logger.error("Failed to add messages: %s", e)

    async def get_messages(self, count: int = 20) -> List[Message]:
        """
        Get the last `count` messages from the session history.
        Args:
            count: Number of messages to retrieve (default is 20).
        Returns:
            List[Message]: List of Message objects from the session history.
        """
        try:
            if self.message_db:
                self.logger.info("Fetching last %d messages from DB", count)
                return await self.message_db.get_messages(self.user_id, self.session_id, count)
            key = (self.user_id, self.session_id)
            self.logger.info("Fetching last %d messages from local session", count)
            return Session._local_messages[key][-count:]
        except Exception as e:
            self.logger.error("Failed to get history: %s", e)
            return []

    async def clear_session(self) -> None:
        """
        Clear the session history.
        This will remove all messages from the session.
        """
        try:
            if self.message_db:
                self.logger.info("Clearing history in DB")
                await self.message_db.clear_history(self.user_id, self.session_id)
            else:
                key = (self.user_id, self.session_id)
                self.logger.info("Clearing local session history")
                Session._local_messages[key] = []
        except Exception as e:
            self.logger.error("Failed to clear history: %s", e)

    async def pop_message(self) -> Optional[Message]:
        """
        Pop the last message from the session history.
        This will remove the last message from the session and return it.
        If the last message is a tool result, it will continue popping until a non-tool result message is found.
        If no such message exists, it returns None.
        Returns:
            Optional[Message]: The last non-tool result message or None if no such message exists.
        """
        try:
            if self.message_db:
                self.logger.info("Popping last message from DB")
                return await self.message_db.pop_message(self.user_id, self.session_id)
            else:
                key = (self.user_id, self.session_id)
                self.logger.info("Popping last message from local session")
                while Session._local_messages[key]:
                    msg = Session._local_messages[key].pop()
                    if not msg.tool_call:
                        return msg
                return None
        except Exception as e:
            self.logger.error("Failed to pop message: %s", e)
            return None
