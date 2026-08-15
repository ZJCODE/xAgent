"""Shared inbound turn payload for channel adapters.

Adapters convert native messages into ``ChatTurnRequest`` and unpack it into
``Agent.chat_events`` or ``Agent.submit``. This is a typing/composition seam,
not a new runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, Union

from ..core.delivery import DeliveryContext, DeliverySession
from ..core.inbox import InboxKind


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
        return kwargs


class Channel(Protocol):
    """Duck-typed channel adapter surface used by heartbeat and CLI."""

    @property
    def channel_name(self) -> str:
        ...

    def open_delivery_session(self, context: DeliveryContext) -> DeliverySession:
        ...
