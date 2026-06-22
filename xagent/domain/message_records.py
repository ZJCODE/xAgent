"""Shared message-store value types."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Union

from .messages import Message

MessageBatch = Union[Message, Sequence[Message]]


@dataclass(frozen=True)
class StoredMessage:
    """A persisted message plus its stable store id."""

    id: int
    message: Message
