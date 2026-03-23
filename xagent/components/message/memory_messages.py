"""In-memory message storage for private/ephemeral conversations."""

from typing import List, Optional, Union

from .base_messages import MessageStorageBase
from ...schemas import Message


class MessageStorageInMemory(MessageStorageBase):
    """Ephemeral message storage backed by an in-memory list.

    Used for private mode where messages should not be persisted.
    All data is lost when the instance is discarded.
    """

    def __init__(self) -> None:
        self._messages: List[Message] = []

    async def add_messages(
        self,
        messages: Union[Message, List[Message]],
        **kwargs,
    ) -> None:
        if isinstance(messages, list):
            self._messages.extend(messages)
        else:
            self._messages.append(messages)

    async def get_messages(
        self,
        count: int = 20,
        offset: int = 0,
    ) -> List[Message]:
        if count <= 0:
            return []
        end = len(self._messages) - offset
        start = max(0, end - count)
        if end <= 0:
            return []
        return list(self._messages[start:end])

    async def clear_messages(self) -> None:
        self._messages.clear()

    async def pop_message(self) -> Optional[Message]:
        if not self._messages:
            return None
        return self._messages.pop()

    async def get_message_count(self) -> int:
        return len(self._messages)
