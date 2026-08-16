"""Shared inbound turn payload for channel adapters.

Adapters convert native messages into ``ChatTurnRequest`` and unpack it into
``Agent.chat_events``. This is a typing/composition seam, not a new runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Union

from ..core.inbox import InboxKind
from ..core.runtime.subconscious import SubconsciousDelivery


@dataclass
class ChatTurnRequest:
    """Normalized arguments for one waking agent turn."""

    user_message: str
    user_id: str
    channel: str
    room_name: Optional[str] = None
    channel_instructions: str = ""
    attachments: Optional[list[dict[str, Any]]] = None
    image_source: Optional[Union[str, list[str]]] = None
    stream: bool = False
    inbox_kind: str = InboxKind.USER_TURN.value
    sender_name: str = ""

    def to_chat_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user_message": self.user_message,
            "user_id": self.user_id,
            "channel": self.channel,
            "inbox_kind": self.inbox_kind,
        }
        if self.stream:
            kwargs["stream"] = True
        if self.room_name:
            kwargs["room_name"] = self.room_name
        if self.channel_instructions:
            kwargs["channel_instructions"] = self.channel_instructions
        if self.attachments:
            kwargs["attachments"] = self.attachments
        if self.image_source is not None:
            kwargs["image_source"] = self.image_source
        if self.sender_name:
            kwargs["sender_name"] = self.sender_name
        return kwargs


class Channel(Protocol):
    """Duck-typed channel adapter surface used by heartbeat and CLI.

    Adapters map native inbound payloads to ``ChatTurnRequest`` (typically a
    private ``_chat_kwargs`` helper) rather than sharing one inbound type.
    """

    @property
    def channel_name(self) -> str:
        ...

    async def deliver_subconscious_message(self, delivery: SubconsciousDelivery) -> None:
        ...
