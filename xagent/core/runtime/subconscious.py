"""Subconscious subconscious loop for autonomous agent thought generation."""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import AgentConfig
from .scheduler import _fsync_directory

logger = logging.getLogger(__name__)

SUBCONSCIOUS_SOURCE = "subconscious"
SUBCONSCIOUS_EVENT_TYPE = "internal_monologue"
INTERNAL_MARKER = "internal"
CONTACTS_FILENAME = "contacts.json"


@dataclass(frozen=True)
class ContactEntry:
    """A single contact entry in the persistent contacts registry."""

    channel: str
    user_id: str
    target: Dict[str, Any]
    last_seen: str  # ISO-format timestamp
    interaction_count: int = 0


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


def upsert_contact(
    contacts_file: Path,
    channel: str,
    user_id: str,
    target: Dict[str, Any],
) -> None:
    """Record or update a contact after a user interaction."""
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


def resolve_subconscious_tasks_dir(workspace: Path) -> Path:
    """Resolve the subconscious tasks directory inside the workspace."""
    return workspace / AgentConfig.SUBCONSCIOUS_TASKS_DIRNAME


class SubconsciousLoop:
    """Periodic subconscious thought loop for the agent.

    Each heartbeat tick has a small probability of triggering an
    subconscious event.  The agent generates a spontaneous thought and
    decides whether it is worth sharing.  Thoughts that are not shared
    are recorded as internal monologue (context events with the
    ``[internal]`` marker).  Thoughts worth sharing are enqueued as
    scheduled tasks in a dedicated ``subconscious_tasks/`` directory,
    isolated from user-created tasks.
    """

    def __init__(
        self,
        agent: Any,
        *,
        workspace: Path,
        probability: Optional[float] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._agent = agent
        self._workspace = Path(workspace).expanduser().resolve()
        self._contacts_file = resolve_contacts_path(self._workspace)
        self._subconscious_tasks_dir = resolve_subconscious_tasks_dir(self._workspace)
        self._logger = logger_ or logger
        self._enabled = AgentConfig.SUBCONSCIOUS_ENABLED
        self._probability = (
            float(probability)
            if probability is not None
            else float(AgentConfig.SUBCONSCIOUS_ACTIVITY)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def contacts_file(self) -> Path:
        return self._contacts_file

    @property
    def subconscious_tasks_dir(self) -> Path:
        return self._subconscious_tasks_dir

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

        worthy = bool(result.get("worthy"))
        content = str(result.get("content") or "").strip()
        reasoning = str(result.get("reasoning") or "").strip()
        recipient_hint = result.get("recipient_hint")

        self._logger.info(
            "Subconscious result: worthy=%s content=%.80s...", worthy, content
        )

        if not content:
            return

        if not worthy:
            await self._write_internal_thought(content, reasoning)
        else:
            await self._route_subconscious_thought(content, reasoning, recipient_hint)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_subconscious_thought(self) -> Dict[str, Any]:
        """Call the LLM to generate a subconscious thought."""
        recent_messages = await self._collect_recent_messages()
        memory_context = await self._collect_memory_context()
        contacts_summary = self._collect_contacts_summary()

        parts: List[str] = []
        if recent_messages:
            parts.append(f"**Recent experience:**\n{recent_messages}")
        parts.append(f"**Recent memories:**\n{memory_context}")
        parts.append(f"**People you interact with:**\n{contacts_summary}")
        parts.append("Generate a spontaneous thought now.")
        prompt = "\n\n".join(parts)

        core_rules = (
            "\n"
            + AgentConfig.BASE_AGENT_RULES_HEADER
            + AgentConfig.BASE_AGENT_CORE_IDENTITY
            + AgentConfig.BASE_AGENT_SELF_RULES
            + AgentConfig.BASE_AGENT_BOUNDARY_RULES
            + AgentConfig.BASE_AGENT_CONTEXT_RULES
            + AgentConfig.BASE_AGENT_RULES_FOOTER
        )
        instructions = [
            {"role": "system", "content": AgentConfig.SUBCONSCIOUS_SYSTEM_PROMPT},
            {"role": "system", "content": core_rules},
        ]
        if self._agent.system_prompt.strip():
            instructions.append({
                "role": "system",
                "name": AgentConfig.IDENTITY_CONTEXT_NAME,
                "content": AgentConfig.build_identity_context(self._agent.system_prompt),
            })

        input_messages = [{
            "role": "user",
            "content": prompt,
        }]

        model_client = getattr(self._agent, "model_client", None)
        if model_client is None:
            raise RuntimeError("Agent has no model_client")

        from ..config import ReplyType

        reply_type, payload = await model_client.call(
            messages=input_messages,
            tool_specs=None,
            instructions=instructions,
            stream=False,
        )
        if reply_type != ReplyType.SIMPLE_REPLY:
            raise RuntimeError(f"Subconscious call returned non-text: {reply_type}")

        text = str(payload).strip()
        return self._parse_subconscious_json(text)

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
            return {"worthy": False, "content": text[:500], "reasoning": "json parse failed"}
        if not isinstance(result, dict):
            return {"worthy": False, "content": str(result)[:500], "reasoning": "non-dict result"}
        return result

    async def _collect_memory_context(self) -> str:
        """Collect recent memory for subconscious context."""
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None:
            return "(no memory available)"
        try:
            ctx = await memory_handler.get_recent_context()
            return ctx.strip() if ctx else "(no recent memory)"
        except Exception:
            self._logger.warning("Failed to collect memory context", exc_info=True)
            return "(memory read failed)"

    def _collect_contacts_summary(self) -> str:
        """Summarize known contacts for the subconscious prompt."""
        contacts = load_contacts(self._contacts_file)
        if not contacts:
            return "(no contacts recorded yet)"
        lines: List[str] = []
        for c in contacts:
            name = c.target.get("sender_name") or c.user_id or "unknown"
            lines.append(f"- {name} via {c.channel} (last seen {c.last_seen}, {c.interaction_count} interactions)")
        return "\n".join(lines)

    async def _collect_recent_messages(self) -> str:
        """Collect recent conversation messages for subconscious context."""
        message_handler = getattr(self._agent, "message_handler", None)
        if message_handler is None:
            return ""
        try:
            from ..handlers.message import MessageHandler

            limit = AgentConfig.SUBCONSCIOUS_MAX_RECENT_MESSAGES
            messages = await message_handler.get_recent_messages(max_history=limit)
            if not messages:
                return ""
            lines: List[str] = []
            for msg in messages:
                header = MessageHandler._format_transcript_message_header(msg)
                lines.append(f"{header}\n{msg.content.strip()}")
            return "\n\n".join(lines)
        except Exception:
            self._logger.warning("Failed to collect recent messages", exc_info=True)
            return ""

    async def _write_internal_thought(self, content: str, reasoning: str) -> None:
        """Record the thought as an internal monologue context event."""
        record_method = getattr(self._agent, "record_internal_thought", None)
        if callable(record_method):
            await record_method(content, reasoning=reasoning)
            self._logger.info("Internal thought recorded")
            return
        # Fallback: store directly via message_handler
        message_handler = getattr(self._agent, "message_handler", None)
        if message_handler is None:
            return
        from ...schemas import Message

        msg = Message.create_context_event(
            content=f"[{INTERNAL_MARKER}] {content}",
            source=SUBCONSCIOUS_SOURCE,
            event_type=SUBCONSCIOUS_EVENT_TYPE,
            metadata={"reasoning": reasoning},
        )
        store = getattr(message_handler, "store_message", None)
        if callable(store):
            await store(msg)

    async def _route_subconscious_thought(
        self,
        content: str,
        reasoning: str,
        recipient_hint: Any,
    ) -> None:
        """Route a worthy thought: send now, or write as internal thought.

        During quiet hours (22:00 – 8:00) the thought is recorded as an
        internal monologue instead of being delivered — the agent's sleep
        thoughts become part of its memory without disturbing the user.
        """
        contacts = load_contacts(self._contacts_file)
        recipient = self._pick_recipient(contacts, recipient_hint)

        if recipient is None:
            self._logger.info("No suitable recipient – recording as internal thought")
            await self._write_internal_thought(content, reasoning)
            return

        now = datetime.now()
        if not self._is_appropriate_time(now):
            # Nighttime – don't disturb; let the thought become a memory
            self._logger.info(
                "Quiet hours – recording subconscious thought as internal thought"
            )
            await self._write_internal_thought(content, reasoning)
            return

        self._subconscious_tasks_dir.mkdir(parents=True, exist_ok=True)
        from .tasks import enqueue_scheduled_task

        enqueue_scheduled_task(
            task_type="message",
            content=content,
            run_at=now,
            tasks_dir=self._subconscious_tasks_dir,
            channel=recipient.channel,
            target=recipient.target,
            user_id=recipient.user_id,
            title=f"Subconscious: {content[:60]}",
            source={"source": SUBCONSCIOUS_SOURCE, "reasoning": reasoning},
        )
        self._logger.info(
            "Subconscious thought enqueued: channel=%s user_id=%s run_at=%s",
            recipient.channel,
            recipient.user_id,
            now.isoformat(sep=" "),
        )

    @staticmethod
    def _pick_recipient(
        contacts: List[ContactEntry],
        recipient_hint: Any,
    ) -> Optional[ContactEntry]:
        """Pick the most relevant contact for the thought."""
        if not contacts:
            return None
        # If hint matches a contact name, prefer that
        hint = str(recipient_hint or "").strip().lower()
        if hint:
            for c in contacts:
                name = str(c.target.get("sender_name") or "").lower()
                if hint in name or hint in c.user_id.lower():
                    return c
        # Default: most recently seen contact
        return max(contacts, key=lambda c: c.last_seen)

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

