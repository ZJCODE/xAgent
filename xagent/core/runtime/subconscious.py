"""Persisted subconscious event producer and processor."""

from __future__ import annotations

import inspect
import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from ..config import AgentConfig
from .types import AgentEvent

logger = logging.getLogger(__name__)

@dataclass(frozen=True)
class ContactEntry:
    """One SQLite-backed account that may receive a proactive delivery."""

    channel: str
    user_id: str
    target: Dict[str, Any]
    last_seen: str  # ISO-format timestamp
    interaction_count: int = 0


@dataclass(frozen=True)
class SubconsciousDelivery:
    """An outbound message chosen while processing one persisted event."""

    event_id: str
    content: str
    recipient: ContactEntry
    internal_content: str
    created_at: datetime

class SubconsciousLoop:
    """Submit triggers to, and process them inside, the single cognitive runtime."""

    def __init__(
        self,
        agent: Any,
        *,
        event_sink: Callable[[AgentEvent], Awaitable[None]],
        probability: Optional[float] = None,
        delivery_sink: Optional[Callable[[SubconsciousDelivery], Awaitable[None] | None]] = None,
        before_side_effect: Optional[Callable[[str], Awaitable[None]]] = None,
        contacts_provider: Optional[
            Callable[[], Awaitable[List[ContactEntry]] | List[ContactEntry]]
        ] = None,
        deliverable_channels: Optional[Iterable[str]] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._agent = agent
        self._event_sink = event_sink
        self._delivery_sink = delivery_sink
        self._before_side_effect = before_side_effect
        self._contacts_provider = contacts_provider
        self._deliverable_channels = self._normalize_deliverable_channels(deliverable_channels)
        self._logger = logger_ or logger
        self._probability = (
            float(probability)
            if probability is not None
            else float(AgentConfig.SUBCONSCIOUS_ACTIVITY)
        )

    def should_trigger(self) -> bool:
        """Return whether this heartbeat should enqueue a thought event."""
        return self._probability > 0 and random.random() < self._probability

    async def maybe_submit(self) -> None:
        """Persist a trigger; model work never runs in the heartbeat task."""
        if not self.should_trigger():
            return
        await self._event_sink(
            AgentEvent.create(
                kind="subconscious",
                source="runtime",
                conversation_id="self",
                speaker_id="agent",
                audience_ids=("agent",),
                content="heartbeat",
            )
        )

    async def process_event(self, event: AgentEvent) -> dict[str, Any]:
        """Process one already-persisted trigger inside the Runtime FIFO."""
        result = await self._generate_subconscious_thought()
        internal_content = str(result.get("internal_content") or "").strip()
        external_content = str(result.get("external_content") or "").strip()
        worthy = bool(result.get("worthy"))
        recipient_hint = result.get("recipient_hint")

        if not internal_content and not external_content:
            return {"kind": "subconscious", "worthy": False, "delivery_created": False}

        if internal_content:
            await self._mark_side_effect(event.event_id)
            await self._write_subconscious_thought(internal_content)

        delivery_created = False
        if worthy and external_content:
            delivery_created = await self._route_subconscious_thought(
                event.event_id,
                external_content,
                internal_content,
                recipient_hint,
            )
        return {
            "kind": "subconscious",
            "worthy": worthy,
            "delivery_created": delivery_created,
        }

    async def _generate_subconscious_thought(self) -> Dict[str, Any]:
        """Run a subconscious agent turn and parse the final JSON result."""
        instructions, input_messages, tool_specs = await self._build_subconscious_turn_context()

        model_client = getattr(self._agent, "model_client", None)
        if model_client is None:
            raise RuntimeError("Agent has no model_client")
        if not callable(getattr(model_client, "model_turn_events", None)):
            raise RuntimeError("Agent model_client does not support model_turn_events()")

        max_iter = int(getattr(self._agent, "max_iter", AgentConfig.DEFAULT_MAX_ITER) or AgentConfig.DEFAULT_MAX_ITER)

        for _ in range(max_iter):
            text_parts: List[str] = []
            tool_calls = []
            async for model_event in model_client.model_turn_events(
                messages=input_messages,
                tool_specs=tool_specs,
                instructions=instructions,
                stream=False,
            ):
                if model_event.type in {"delta", "text"} and model_event.delta:
                    text_parts.append(model_event.delta)
                    continue
                if model_event.type == "tool_calls":
                    tool_calls = model_event.tool_calls
                    continue
                if model_event.type == "error":
                    message = getattr(getattr(model_event, "error", None), "message", "")
                    raise RuntimeError(f"Subconscious model error: {message or model_event.error}")

            text = "".join(text_parts).strip()
            if tool_calls:
                self._logger.warning("Subconscious returned tool calls; tools are unavailable for this turn")
                if text:
                    return self._parse_subconscious_json(text)
                raise RuntimeError("Subconscious returned tool calls without text")

            if text:
                return self._parse_subconscious_json(text)

            raise RuntimeError("Subconscious model turn ended without text or tool calls")

        raise RuntimeError(f"Subconscious thought failed after {max_iter} attempts")

    async def _build_subconscious_turn_context(self) -> tuple[list[dict], list[dict], list]:
        """Build model input using the same layers as a normal agent turn."""
        message_handler = getattr(self._agent, "message_handler", None)
        if message_handler is None:
            raise RuntimeError("Agent has no message_handler")

        recent_messages = await message_handler.get_recent_messages(
            max_history=getattr(self._agent, "max_history", AgentConfig.DEFAULT_MAX_HISTORY)
        )
        memory_context = await self._collect_memory_context()

        instructions = message_handler.build_instruction_messages(
            tool_names=[],
            skills_catalog="",
            supports_vision=bool(getattr(self._agent, "supports_vision", True)),
            is_subconscious=True,
        )
        iteration_messages = message_handler.build_turn_context_messages(
            recent_messages,
            current_user_id=getattr(self._agent, "_assistant_sender_id", "agent"),
            memory_context=memory_context,
            max_messages=getattr(self._agent, "max_history", AgentConfig.DEFAULT_MAX_HISTORY),
            include_images=False,
            workspace_dir=getattr(self._agent, "workspace_dir", None),
            task_mode="subconscious_json",
        )
        input_messages = message_handler.sanitize_input_messages(list(iteration_messages))
        return instructions, input_messages, []

    @staticmethod
    def _parse_subconscious_json(text: str) -> Dict[str, Any]:
        """Parse subconscious JSON from LLM output, robust to code fences."""
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            end = None
            for i in range(len(lines) - 1, 0, -1):
                if lines[i].strip() == "```":
                    end = i
                    break
            if end is not None:
                cleaned = "\n".join(lines[1:end]).strip()
        try:
            result = json.loads(cleaned)
        except json.JSONDecodeError:
            # Fallback: treat the whole text as an unworthy thought
            return {
                "internal_content": text[:500],
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            }
        if not isinstance(result, dict):
            return {
                "internal_content": str(result)[:500],
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            }
        return result

    async def _collect_memory_context(self) -> str:
        """Collect recent memory for subconscious context."""
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None:
            return "(no memory available)"
        try:
            ctx = memory_handler.get_subconscious_context()
            if inspect.isawaitable(ctx):
                ctx = await ctx
            return ctx.strip() if ctx else "(no recent memory)"
        except Exception:
            self._logger.warning("Failed to collect subconscious memory context", exc_info=True)
            return "(memory read failed)"

    async def _write_subconscious_thought(self, content: str) -> None:
        """Record the raw inner thought in the Markdown diary."""
        record_method = getattr(self._agent, "record_subconscious_thought", None)
        if not callable(record_method):
            raise RuntimeError("Agent cannot record subconscious thoughts")
        await record_method(content)

    async def _route_subconscious_thought(
        self,
        event_id: str,
        external_content: str,
        internal_content: str,
        recipient_hint: Any,
    ) -> bool:
        """Create a durable Delivery for a worthy thought when possible."""
        contacts = self._filter_deliverable_contacts(await self._load_contacts())
        recipient = self._pick_recipient(contacts, recipient_hint)

        if recipient is None:
            self._logger.info("No suitable recipient for subconscious thought")
            return False

        now = datetime.now()
        if not self._is_appropriate_time(now):
            self._logger.info("Quiet hours – skipping subconscious delivery")
            return False

        if self._delivery_sink is None:
            raise RuntimeError("Subconscious delivery sink is not configured")

        delivery = SubconsciousDelivery(
            event_id=event_id,
            content=external_content,
            recipient=recipient,
            internal_content=internal_content,
            created_at=now,
        )
        await self._mark_side_effect(event_id)
        result = self._delivery_sink(delivery)
        if inspect.isawaitable(result):
            await result
        return True

    async def _load_contacts(self) -> List[ContactEntry]:
        if self._contacts_provider is None:
            return []
        value = self._contacts_provider()
        if inspect.isawaitable(value):
            value = await value
        return list(value)

    async def _mark_side_effect(self, event_id: str) -> None:
        if self._before_side_effect is not None:
            await self._before_side_effect(event_id)

    @staticmethod
    def _pick_recipient(
        contacts: List[ContactEntry],
        recipient_hint: Any,
    ) -> Optional[ContactEntry]:
        """Pick the most relevant contact for the thought."""
        if not contacts:
            return None
        # If hint matches a contact, prefer that
        hint = str(recipient_hint or "").strip().lower()
        if hint:
            # -- pass 1: exact match on name or user_id --
            for c in contacts:
                name = str(c.target.get("sender_name") or "").lower()
                if hint == name or hint == c.user_id.lower():
                    return c
            # -- pass 2: partial match (hint contains name, or name contains
            #    hint).  The hint may carry channel annotations such as
            #    "Telos (feishu)", and user / sender names may be prefixes.
            for c in contacts:
                name = str(c.target.get("sender_name") or "").lower()
                user_id_lower = c.user_id.lower()
                if (
                    (name and (hint in name or name in hint))
                    or (user_id_lower and (hint in user_id_lower or user_id_lower in hint))
                ):
                    return c
            return None
        # Default: most recently seen contact
        return max(contacts, key=lambda c: c.last_seen)

    @staticmethod
    def _normalize_deliverable_channels(channels: Optional[Iterable[str]]) -> set[str]:
        if channels is None:
            return set()
        return {str(channel).strip().lower() for channel in channels if str(channel).strip()}

    def _filter_deliverable_contacts(self, contacts: List[ContactEntry]) -> List[ContactEntry]:
        return [
            contact
            for contact in contacts
            if str(contact.channel or "").strip().lower() in self._deliverable_channels
        ]

    @staticmethod
    def _is_appropriate_time(now: datetime) -> bool:
        """Check whether the current time is appropriate for sending.

        Respects ``AgentConfig.SUBCONSCIOUS_QUIET_HOURS_START`` and
        ``SUBCONSCIOUS_QUIET_HOURS_END`` so users can define their own
        quiet window.
        """
        hour = now.hour
        start = AgentConfig.SUBCONSCIOUS_QUIET_HOURS_START
        end = AgentConfig.SUBCONSCIOUS_QUIET_HOURS_END
        if start <= end:
            # Simple range: e.g. quiet 0–6 (midnight to 6 AM)
            return not (start <= hour < end)
        # Overnight range: e.g. quiet 22–8 (10 PM to 8 AM)
        return not (hour >= start or hour < end)
