"""Subconscious subconscious loop for autonomous agent thought generation."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX platforms
    msvcrt = None

from ..config import AgentConfig
from .scheduler import _fsync_directory

logger = logging.getLogger(__name__)

CONTACTS_FILENAME = "contacts.json"
SUBCONSCIOUS_DELIVERY_RETRIES = 2
SUBCONSCIOUS_DELIVERY_RETRY_DELAY_SECONDS = 0.5


@dataclass(frozen=True)
class ContactEntry:
    """A single contact entry in the persistent contacts registry."""

    channel: str
    user_id: str
    target: Dict[str, Any]
    last_seen: str  # ISO-format timestamp
    interaction_count: int = 0


@dataclass(frozen=True)
class SubconsciousDelivery:
    """A direct outbound message chosen by the subconscious loop."""

    content: str
    recipient: ContactEntry
    internal_content: str
    created_at: datetime


def load_contacts(contacts_file: Path) -> List[ContactEntry]:
    """Load contacts from the persistent JSON registry."""
    if not contacts_file.is_file():
        return []
    try:
        raw = json.loads(contacts_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    entries = raw.get("contacts")
    if not isinstance(entries, list):
        return []
    result: List[ContactEntry] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        try:
            result.append(ContactEntry(
                channel=str(item.get("channel", "")),
                user_id=str(item.get("user_id", "")),
                target=dict(item.get("target") or {}),
                last_seen=str(item.get("last_seen", "")),
                interaction_count=int(item.get("interaction_count", 0)),
            ))
        except (TypeError, ValueError):
            continue
    return result


def save_contacts(contacts_file: Path, contacts: List[ContactEntry]) -> None:
    """Persist contacts to the JSON registry (atomic write)."""
    contacts_file.parent.mkdir(parents=True, exist_ok=True)
    max_contacts = max(1, AgentConfig.SUBCONSCIOUS_MAX_CONTACTS)
    trimmed = sorted(contacts, key=lambda c: c.last_seen, reverse=True)[:max_contacts]
    payload = {
        "contacts": [
            {
                "channel": c.channel,
                "user_id": c.user_id,
                "target": c.target,
                "last_seen": c.last_seen,
                "interaction_count": c.interaction_count,
            }
            for c in trimmed
        ]
    }
    tmp_path = contacts_file.with_name(f".{contacts_file.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, contacts_file)
    _fsync_directory(contacts_file.parent)


@contextmanager
def _contacts_process_lock(contacts_file: Path):
    """Cross-process exclusive lock protecting contacts.json read-modify-write.

    Uses the same flock / msvcrt pattern as MemoryHandler so the lock is
    automatically released if the process exits or crashes.
    """
    lock_path = contacts_file.with_name(contacts_file.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        else:
            lock_handle.seek(0)
            if not lock_handle.read(1):
                lock_handle.write("\0")
                lock_handle.flush()
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_LOCK, 1)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        else:
            lock_handle.seek(0)
            msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
        lock_handle.close()


def upsert_contact(
    contacts_file: Path,
    channel: str,
    user_id: str,
    target: Dict[str, Any],
) -> None:
    """Record or update a contact after a user interaction."""
    with _contacts_process_lock(contacts_file):
        contacts = load_contacts(contacts_file)
        now_iso = datetime.now().replace(microsecond=0).isoformat(sep=" ")
        updated = False
        for c in contacts:
            if c.channel == channel and c.user_id == user_id:
                # Update in place by rebuilding the list
                updated = True
                break
        if updated:
            contacts = [
                ContactEntry(
                    channel=channel,
                    user_id=user_id,
                    target=dict(target),
                    last_seen=now_iso,
                    interaction_count=c.interaction_count + 1,
                )
                if c.channel == channel and c.user_id == user_id
                else c
                for c in contacts
            ]
        else:
            contacts.append(ContactEntry(
                channel=channel,
                user_id=user_id,
                target=dict(target),
                last_seen=now_iso,
                interaction_count=1,
            ))
        save_contacts(contacts_file, contacts)


def resolve_contacts_path(workspace: Path) -> Path:
    """Resolve the contacts JSON file path inside the workspace."""
    return workspace / CONTACTS_FILENAME


class SubconsciousLoop:
    """Periodic subconscious thought loop for the agent.

    Each heartbeat tick has a small probability of triggering an
    subconscious event.  The agent generates a spontaneous thought and
    decides whether it is worth sharing. Raw inner thoughts are written
    directly to the diary, and thoughts worth sharing are handed to the
    runtime's direct delivery sink.
    """

    def __init__(
        self,
        agent: Any,
        *,
        workspace: Path,
        probability: Optional[float] = None,
        delivery_sink: Optional[Callable[[SubconsciousDelivery], Awaitable[None] | None]] = None,
        deliverable_channels: Optional[Iterable[str]] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._agent = agent
        self._workspace = Path(workspace).expanduser().resolve()
        self._contacts_file = resolve_contacts_path(self._workspace)
        self._delivery_sink = delivery_sink
        self._deliverable_channels = self._normalize_deliverable_channels(deliverable_channels)
        self._logger = logger_ or logger
        self._enabled = AgentConfig.SUBCONSCIOUS_ENABLED
        self._probability = (
            float(probability)
            if probability is not None
            else float(AgentConfig.SUBCONSCIOUS_ACTIVITY)
        )
        self._delivery_retries = SUBCONSCIOUS_DELIVERY_RETRIES
        self._delivery_retry_delay_seconds = SUBCONSCIOUS_DELIVERY_RETRY_DELAY_SECONDS

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def contacts_file(self) -> Path:
        return self._contacts_file

    def record_interaction(
        self,
        channel: str,
        user_id: str,
        target: Dict[str, Any],
    ) -> None:
        """Record a user interaction for future subconscious routing.

        Called by channel adapters after every incoming user message.
        """
        try:
            upsert_contact(
                self._contacts_file,
                channel=channel,
                user_id=user_id,
                target=target,
            )
        except Exception:
            self._logger.warning(
                "Failed to record interaction for subconscious: channel=%s user_id=%s",
                channel,
                user_id,
                exc_info=True,
            )

    def should_trigger(self) -> bool:
        """Return True if subconscious thought should fire this tick (2% dice roll)."""
        if not self._enabled:
            return False
        return random.random() < self._probability

    async def maybe_think(self) -> None:
        """Run one subconscious cycle if the dice roll passes."""
        if not self.should_trigger():
            return

        self._logger.info("Subconscious thought triggered – generating thought")
        try:
            result = await self._generate_subconscious_thought()
        except Exception:
            self._logger.exception("Subconscious thought generation failed")
            return

        internal_content = str(result.get("internal_content") or "").strip()
        external_content = str(result.get("external_content") or "").strip()
        worthy = bool(result.get("worthy"))
        recipient_hint = result.get("recipient_hint")

        self._logger.info(
            "Subconscious result: worthy=%s internal=%.80s... external=%.80s...",
            worthy,
            internal_content,
            external_content,
        )

        if not internal_content and not external_content:
            return

        if internal_content:
            await self._write_subconscious_thought(internal_content)

        if not worthy:
            return

        if not external_content:
            return

        await self._route_subconscious_thought(
            external_content,
            internal_content,
            recipient_hint,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
        relationship_context = await self._collect_relationship_context()

        instructions = message_handler.build_instruction_messages(
            tool_names=[],
            skills_catalog="",
            supports_vision=bool(getattr(self._agent, "supports_vision", True)),
            workspace_context="",
        )
        iteration_messages = message_handler.build_turn_context_messages(
            recent_messages,
            current_user_id=getattr(self._agent, "_assistant_sender_id", "agent"),
            memory_context=memory_context,
            relationship_context=relationship_context,
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

    async def _collect_relationship_context(self) -> str:
        """Collect relationship cards to ground subconscious thought."""
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None or not callable(
            getattr(memory_handler, "get_relationship_context", None)
        ):
            return ""
        contacts = self._filter_deliverable_contacts(load_contacts(self._contacts_file))
        from ...components.memory import RelationshipStore

        keys: list[str] = []
        for contact in contacts:
            self._append_unique_key(keys, RelationshipStore.make_key(contact.channel, contact.user_id))

        relationship_store = getattr(memory_handler, "relationship_store", None)
        list_keys = getattr(relationship_store, "list_keys", None)
        if callable(list_keys):
            try:
                stored_keys = list_keys()
                if inspect.isawaitable(stored_keys):
                    stored_keys = await stored_keys
                if isinstance(stored_keys, list):
                    for key in stored_keys:
                        self._append_unique_key(keys, str(key))
            except Exception:
                self._logger.warning("Failed to list relationship cards for subconscious", exc_info=True)

        if not keys:
            return ""
        try:
            return await memory_handler.get_relationship_context(
                speaker_keys=keys,
                max_cards=AgentConfig.RELATIONSHIP_SUBCONSCIOUS_MAX_CARDS,
                include_routing_id=True,
            )
        except Exception:
            self._logger.warning("Failed to collect relationship context", exc_info=True)
            return ""

    @staticmethod
    def _append_unique_key(keys: list[str], key: str) -> None:
        normalized = (key or "").strip()
        if normalized and normalized not in keys:
            keys.append(normalized)

    async def _write_subconscious_thought(self, content: str) -> None:
        """Record the raw inner thought directly in the diary."""
        record_method = getattr(self._agent, "record_subconscious_thought", None)
        if callable(record_method):
            try:
                await record_method(content)
                self._logger.info("Subconscious thought recorded in diary")
            except Exception:
                self._logger.warning("Failed to record subconscious thought in diary", exc_info=True)
            return

        memory = getattr(self._agent, "markdown_memory", None)
        append_daily = getattr(memory, "append_daily", None)
        if callable(append_daily):
            try:
                await append_daily(content.strip())
                self._logger.info("Subconscious thought recorded in diary")
            except Exception:
                self._logger.warning("Failed to record subconscious thought in diary", exc_info=True)

    async def _route_subconscious_thought(
        self,
        external_content: str,
        internal_content: str,
        recipient_hint: Any,
    ) -> None:
        """Route a worthy thought for direct delivery when possible.

        During quiet hours (22:00-8:00), delivery is skipped to avoid
        disturbing the user. The inner thought has already been written
        to the diary by the caller.
        """
        contacts = self._filter_deliverable_contacts(load_contacts(self._contacts_file))
        recipient = self._pick_recipient(contacts, recipient_hint)

        if recipient is None:
            self._logger.info("No suitable recipient for subconscious thought")
            return

        now = datetime.now()
        if not self._is_appropriate_time(now):
            self._logger.info("Quiet hours – skipping subconscious delivery")
            return

        if self._delivery_sink is None:
            self._logger.info("No subconscious delivery sink configured")
            return

        delivery = SubconsciousDelivery(
            content=external_content,
            recipient=recipient,
            internal_content=internal_content,
            created_at=now,
        )
        try:
            await self._deliver_with_retries(delivery)
            self._logger.info(
                "Subconscious thought delivered: channel=%s user_id=%s created_at=%s",
                recipient.channel,
                recipient.user_id,
                now.isoformat(sep=" "),
            )
        except Exception:
            self._logger.warning("Subconscious delivery failed", exc_info=True)

    async def _deliver_with_retries(self, delivery: SubconsciousDelivery) -> None:
        """Deliver a subconscious thought, retrying transient sink failures."""
        if self._delivery_sink is None:
            raise RuntimeError("Subconscious delivery sink is not configured")

        attempts = max(1, int(self._delivery_retries) + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = self._delivery_sink(delivery)
                if inspect.isawaitable(result):
                    await result
                return
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                self._logger.warning(
                    "Subconscious delivery attempt %s/%s failed; retrying",
                    attempt,
                    attempts,
                    exc_info=True,
                )
                delay = max(0.0, float(self._delivery_retry_delay_seconds))
                if delay:
                    await asyncio.sleep(delay * attempt)

        if last_error is not None:
            raise last_error

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
