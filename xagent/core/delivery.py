"""Unified outbound delivery: one mouth for every model-generated message.

Channels implement ``DeliverySession``. Core drives events, waits for an ack,
and only then commits assistant history.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

logger = logging.getLogger(__name__)


@dataclass
class DeliveryContext:
    """Generic routing context attached to an inbox item.

    ``target`` holds only non-sensitive addressing the channel already needs
    (for example a Feishu ``chat_id``). Tokens and other secrets stay in
    channel-owned state, never here.
    """

    channel: str = ""
    user_id: str = ""
    recipient_key: str = ""
    display_name: str = ""
    target: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def normalized_channel(self) -> str:
        return str(self.channel or "").strip().lower()

    @classmethod
    def from_route(
        cls,
        route: Any,
        *,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> "DeliveryContext":
        return cls(
            channel=str(getattr(route, "channel", "") or ""),
            user_id=str(getattr(route, "user_id", "") or ""),
            recipient_key=str(getattr(route, "recipient_key", "") or ""),
            display_name=str(getattr(route, "display_name", "") or ""),
            target=dict(getattr(route, "target", None) or {}),
            metadata=dict(metadata or {}),
        )


@dataclass(frozen=True)
class DeliveryAck:
    """Channel confirmation for one consumed event."""

    accepted: bool
    error: str = ""


class DeliverySession(Protocol):
    """Channel-owned outbound session for one waking turn."""

    def can_deliver(self) -> bool:
        ...

    async def consume(self, event: dict) -> DeliveryAck:
        ...

    async def aclose(self) -> None:
        ...


class ImmediateDeliverySession:
    """Always-available session used by tests, CLI, and chat_events.

    Consume is a no-op ack. Persistence still happens after the caller
    finishes handling the yielded ``message_done`` event, so a raising
    send in the consumer skips assistant history.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []

    def can_deliver(self) -> bool:
        return True

    async def consume(self, event: dict) -> DeliveryAck:
        self.events.append(dict(event))
        return DeliveryAck(accepted=True)

    async def aclose(self) -> None:
        return None


class UnavailableDeliverySession:
    """Fail-closed session when this process has no mouth for the channel.

    ImmediateDeliverySession would ack without sending and leave a fake
    assistant history. Initiative and scheduled turns must not do that.
    """

    def __init__(self, channel: str = "", reason: str = "") -> None:
        self.channel = str(channel or "").strip()
        self.reason = reason or (
            f"this process cannot deliver to {self.channel or 'unknown'}"
        )
        self.events: list[dict] = []

    def can_deliver(self) -> bool:
        return False

    async def consume(self, event: dict) -> DeliveryAck:
        self.events.append(dict(event))
        if event.get("type") != "message_done":
            return DeliveryAck(accepted=True)
        return DeliveryAck(accepted=False, error=self.reason)

    async def aclose(self) -> None:
        return None


class RejectingDeliverySession:
    """Test double: reachable check may pass, but message_done is refused."""

    def __init__(self, *, can_deliver: bool = True) -> None:
        self._can_deliver = can_deliver
        self.events: list[dict] = []

    def can_deliver(self) -> bool:
        return self._can_deliver

    async def consume(self, event: dict) -> DeliveryAck:
        self.events.append(dict(event))
        if event.get("type") == "message_done":
            return DeliveryAck(accepted=False, error="delivery rejected")
        return DeliveryAck(accepted=True)

    async def aclose(self) -> None:
        return None


ChannelSessionOpener = Callable[[DeliveryContext], Awaitable[DeliverySession] | DeliverySession]


class DeliveryCoordinator:
    """Resolves a channel session for a delivery context.

    Heartbeat and the subconscious never receive a session. Only a waking
    turn, after it holds the inbox lease, may open one.
    """

    def __init__(self) -> None:
        self._openers: dict[str, ChannelSessionOpener] = {}

    def register(self, channel: str, opener: ChannelSessionOpener) -> None:
        key = str(channel or "").strip().lower()
        if not key:
            raise ValueError("delivery channel name is required")
        self._openers[key] = opener

    def unregister(self, channel: str) -> None:
        self._openers.pop(str(channel or "").strip().lower(), None)

    def has_channel(self, channel: str) -> bool:
        return str(channel or "").strip().lower() in self._openers

    async def open_session(self, context: Optional[DeliveryContext]) -> DeliverySession:
        if context is None:
            return ImmediateDeliverySession()
        channel = context.normalized_channel()
        opener = self._openers.get(channel) if channel else None
        if opener is None:
            source = str((context.metadata or {}).get("source") or "").strip()
            if source in {"initiative", "scheduled_task"}:
                logger.warning(
                    "No delivery opener for %s in this process; refusing %s",
                    channel or "unknown",
                    source,
                )
                return UnavailableDeliverySession(channel=channel)
            return ImmediateDeliverySession()
        session = opener(context)
        if inspect.isawaitable(session):
            session = await session
        return session

    async def close_session(self, session: Optional[DeliverySession]) -> None:
        if session is None:
            return
        closer = getattr(session, "aclose", None)
        if not callable(closer):
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.debug("Delivery session close failed", exc_info=True)
