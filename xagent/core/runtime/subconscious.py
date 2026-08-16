"""Subconscious subconscious loop for autonomous agent thought generation."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import random
import time
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
        self._last_experience_cursor: Optional[int] = None
        self._stale_streak = 0
        self._habituation_anchor_mono: Optional[float] = None
        self._recovery_seconds = max(
            0.0, float(AgentConfig.SUBCONSCIOUS_HABITUATION_RECOVERY_SECONDS)
        )

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
        """Return True if subconscious thought should fire this tick."""
        if not self._enabled:
            return False
        return random.random() < self._effective_probability()

    def _effective_probability(self) -> float:
        """``activity × 0.5^stale_streak`` — no floor; life can reset or recover streak."""
        probability = max(0.0, min(1.0, float(self._probability)))
        if self._stale_streak <= 0 or probability <= 0.0:
            return probability
        return probability * (0.5 ** int(self._stale_streak))

    async def maybe_think(self) -> None:
        """Run one subconscious cycle if the dice roll passes.

        Intensity is ``subconscious_activity``. Habituation halves the rate
        after empty/unworthy reflections; new messages clear the streak;
        solitude slowly recovers it so alone time still counts as life.
        """
        experience_cursor, experience_moved = await self._experience_state()
        if experience_moved:
            self._clear_habituation()
        else:
            self._recover_habituation_from_solitude()

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
            self._habituate(experience_cursor)
            return

        diary_note = internal_content
        if worthy and external_content:
            delivered = await self._route_subconscious_thought(
                external_content,
                internal_content,
                recipient_hint,
            )
            if not delivered:
                diary_note = self._held_back_diary_note(internal_content)

        if diary_note:
            await self._write_subconscious_thought(diary_note)

        if worthy:
            self._clear_habituation()
            self._last_experience_cursor = experience_cursor
        else:
            self._habituate(experience_cursor)

    def _clear_habituation(self) -> None:
        self._stale_streak = 0
        self._habituation_anchor_mono = time.monotonic()

    def _habituate(self, experience_cursor: int) -> None:
        """One more private reflection without movement — quiet down next time."""
        self._stale_streak += 1
        self._last_experience_cursor = experience_cursor
        self._habituation_anchor_mono = time.monotonic()

    def _recover_habituation_from_solitude(self) -> None:
        """Let alone time reduce streak so life is not message-gated."""
        if self._stale_streak <= 0 or self._recovery_seconds <= 0:
            return
        if self._habituation_anchor_mono is None:
            self._habituation_anchor_mono = time.monotonic()
            return
        elapsed = time.monotonic() - self._habituation_anchor_mono
        steps = int(elapsed // self._recovery_seconds)
        if steps <= 0:
            return
        self._stale_streak = max(0, self._stale_streak - steps)
        self._habituation_anchor_mono += steps * self._recovery_seconds

    async def _experience_state(self) -> tuple[int, bool]:
        """Return ``(cursor, moved)`` for whether external experience changed.

        Without a message cursor, the first in-process cycle counts as movement
        so later idle ticks can accumulate habituation.
        """
        raw_cursor = await self._current_experience_cursor()
        if raw_cursor is None:
            moved = self._last_experience_cursor is None
            cursor = 0 if self._last_experience_cursor is None else int(self._last_experience_cursor)
            return cursor, moved
        moved = (
            self._last_experience_cursor is None
            or int(raw_cursor) != int(self._last_experience_cursor)
        )
        return int(raw_cursor), moved

    async def _current_experience_cursor(self) -> Optional[int]:
        """Best-effort message cursor used to detect whether life moved on."""
        for owner_name in ("message_storage", "message_handler"):
            owner = getattr(self._agent, owner_name, None)
            if owner is None:
                continue
            getter = getattr(owner, "get_latest_message_cursor", None)
            if not callable(getter):
                storage = getattr(owner, "message_storage", None)
                getter = getattr(storage, "get_latest_message_cursor", None) if storage else None
            if not callable(getter):
                continue
            try:
                cursor = getter()
                if inspect.isawaitable(cursor):
                    cursor = await cursor
                return int(cursor)
            except Exception:
                self._logger.debug(
                    "Failed to read experience cursor from %s",
                    owner_name,
                    exc_info=True,
                )
        return None

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

        hot_window = getattr(self._agent, "max_history", AgentConfig.DEFAULT_MAX_HISTORY)
        recent_messages = await message_handler.get_recent_messages(
            max_history=AgentConfig.history_fetch_depth(hot_window)
        )
        memory_context = await self._collect_memory_context()
        relationship_context = await self._collect_relationship_context()

        instructions = message_handler.build_instruction_messages(
            tool_names=[],
            skills_catalog="",
            supports_vision=bool(getattr(self._agent, "supports_vision", True)),
            workspace_context="",
            is_subconscious=True,
        )
        iteration_messages = message_handler.build_turn_context_messages(
            recent_messages,
            current_user_id=getattr(self._agent, "_assistant_sender_id", "agent"),
            memory_context=memory_context,
            relationship_context=relationship_context,
            max_messages=hot_window,
            include_images=False,
            workspace_dir=getattr(self._agent, "workspace_dir", None),
            task_mode="subconscious_json",
            prompt_registry=getattr(message_handler, "prompt_registry", None),
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
        parsed = SubconsciousLoop._load_json_object(cleaned)
        if parsed is None:
            # Fallback: treat the whole text as an unworthy thought
            return {
                "internal_content": text[:500],
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            }
        if not isinstance(parsed, dict):
            return {
                "internal_content": str(parsed)[:500],
                "worthy": False,
                "recipient_hint": None,
                "external_content": None,
            }
        return SubconsciousLoop._normalize_subconscious_result(parsed)

    @staticmethod
    def _load_json_object(text: str) -> Any:
        """Load a JSON value, or the first embedded object if the model added prose."""
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                obj, _end = decoder.raw_decode(text[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
        return None

    @staticmethod
    def _normalize_subconscious_result(result: Dict[str, Any]) -> Dict[str, Any]:
        """Accept common field aliases and string booleans from model output."""
        worthy_raw = result.get("worthy")
        if isinstance(worthy_raw, str):
            worthy = worthy_raw.strip().lower() in {"true", "1", "yes"}
        else:
            worthy = bool(worthy_raw)
        hint = result.get("recipient_hint")
        if hint is None:
            hint = result.get("recipient")
        external = result.get("external_content")
        if external is None:
            external = result.get("outward_content")
        internal = result.get("internal_content")
        if internal is None:
            internal = result.get("thought")
        return {
            "internal_content": internal,
            "worthy": worthy,
            "recipient_hint": hint,
            "external_content": external,
        }

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
        """Collect relationship cards to ground subconscious thought.

        Cards are people the agent knows, not a send-list. Delivery still
        filters to channels this runtime can reach.
        """
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None or not callable(
            getattr(memory_handler, "get_relationship_context", None)
        ):
            return ""
        contacts = load_contacts(self._contacts_file)
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
    ) -> bool:
        """Deliver a worthy thought. Return True only when the sink accepted it."""
        contacts = self._filter_deliverable_contacts(load_contacts(self._contacts_file))
        contacts = await self._alias_contacts_for_routing(contacts)
        recipient = self._pick_recipient(contacts, recipient_hint)

        if recipient is None:
            self._logger.info("No suitable recipient for subconscious thought")
            return False

        if self._delivery_sink is None:
            self._logger.info("No subconscious delivery sink configured")
            return False

        now = datetime.now()
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
            return True
        except Exception:
            self._logger.warning("Subconscious delivery failed", exc_info=True)
            return False

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

    async def _alias_contacts_for_routing(self, contacts: List[ContactEntry]) -> List[ContactEntry]:
        """Copy relationship display names onto contacts that lack sender_name.

        API / Weixin / voice contacts often store only a raw user_id. The model
        is shown ``## Alice [user_id: web_user]`` and commonly emits ``Alice``
        as ``recipient_hint``, which would otherwise miss the contact.
        """
        if not contacts:
            return contacts
        memory_handler = getattr(self._agent, "memory_handler", None)
        relationship_store = getattr(memory_handler, "relationship_store", None)
        read_cards = getattr(relationship_store, "read_cards", None)
        if not callable(read_cards):
            return contacts
        from ...components.memory import RelationshipStore, human_display_name

        keys = [RelationshipStore.make_key(contact.channel, contact.user_id) for contact in contacts]
        try:
            cards = read_cards(keys)
            if inspect.isawaitable(cards):
                cards = await cards
        except Exception:
            self._logger.warning("Failed to enrich subconscious contacts from relationship cards", exc_info=True)
            return contacts
        if not isinstance(cards, list) or not cards:
            return contacts
        by_key = {card.key: card for card in cards if getattr(card, "key", None)}
        enriched: List[ContactEntry] = []
        for contact in contacts:
            key = RelationshipStore.make_key(contact.channel, contact.user_id)
            card = by_key.get(key)
            display_name = human_display_name(
                getattr(card, "display_name", ""),
                user_id=contact.user_id,
                key=key,
            )
            if not display_name:
                enriched.append(contact)
                continue
            target = dict(contact.target)
            if not str(target.get("sender_name") or "").strip():
                target["sender_name"] = display_name
            if not str(target.get("display_name") or "").strip():
                target["display_name"] = display_name
            enriched.append(
                ContactEntry(
                    channel=contact.channel,
                    user_id=contact.user_id,
                    target=target,
                    last_seen=contact.last_seen,
                    interaction_count=contact.interaction_count,
                )
            )
        return enriched

    @staticmethod
    def _contact_match_tokens(contact: ContactEntry) -> tuple[list[str], list[str]]:
        """Return (exact_tokens, partial_tokens) used to match a recipient hint."""
        exact = [
            contact.user_id,
            str(contact.target.get("sender_name") or ""),
            str(contact.target.get("display_name") or ""),
            str(contact.target.get("room_name") or ""),
            f"{contact.channel}:{contact.user_id}",
        ]
        exact_tokens = [token.strip().lower() for token in exact if str(token).strip()]
        # Composite keys such as ``api:web_user`` are exact-only so a bare
        # channel name cannot absorb every contact on that channel.
        partial_tokens = [token for token in exact_tokens if ":" not in token]
        return exact_tokens, partial_tokens

    @staticmethod
    def _pick_recipient(
        contacts: List[ContactEntry],
        recipient_hint: Any,
    ) -> Optional[ContactEntry]:
        """Pick the named contact. No hint means no send — never guess another person."""
        if not contacts:
            return None
        # If hint matches a contact, prefer that
        hint = str(recipient_hint or "").strip().lower()
        if not hint:
            return None
        token_map = {
            id(contact): SubconsciousLoop._contact_match_tokens(contact)
            for contact in contacts
        }
        # -- pass 1: exact match on name, user_id, or channel:user_id --
        for contact in contacts:
            exact_tokens, _partial = token_map[id(contact)]
            if hint in exact_tokens:
                return contact
        # -- pass 2: the hint may wrap an exact token ("Telos (feishu)").
        # A short hint must not match a longer name ("李" must not hit "李明").
        for contact in contacts:
            _exact, partial_tokens = token_map[id(contact)]
            if any(token and token in hint for token in partial_tokens):
                return contact
        return None

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
    def _held_back_diary_note(internal_content: str) -> str:
        """Record that a worthy thought stayed inside, in the thought's language."""
        thought = (internal_content or "").strip()
        coda = "我没有发出去。" if SubconsciousLoop._looks_cjk(thought) else "I didn't send this."
        if not thought:
            return coda
        if coda in thought:
            return thought
        return f"{thought}\n{coda}"

    @staticmethod
    def _looks_cjk(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)
