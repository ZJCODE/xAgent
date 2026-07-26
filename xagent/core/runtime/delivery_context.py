"""Conversation-local delivery target used when an Agent creates a task."""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass(frozen=True)
class DeliveryContext:
    source: str
    channel: str | None = None
    user_id: str = ""
    target: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[DeliveryContext | None] = contextvars.ContextVar(
    "xagent_delivery_context",
    default=None,
)


def current_delivery_context() -> DeliveryContext | None:
    return _current.get()


@contextlib.contextmanager
def delivery_context(context: DeliveryContext) -> Iterator[None]:
    token = _current.set(context)
    try:
        yield
    finally:
        _current.reset(token)
