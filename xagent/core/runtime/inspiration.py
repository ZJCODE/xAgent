"""Subconscious inspiration loop for autonomous agent thought generation."""

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

INSPIRATION_SOURCE = "inspiration"
INSPIRATION_EVENT_TYPE = "internal_monologue"
INTERNAL_MARKER = "internal"
CONTACTS_FILENAME = "contacts.json"

INSPIRATION_SYSTEM_PROMPT = """\
You are the subconscious of an AI assistant. You are having a spontaneous thought.

Recent memories and context about the people you interact with are provided below.
Generate ONE spontaneous thought or insight. It could be:
- A follow-up question about something discussed earlier
- An interesting observation or connection you just made
- A gentle reminder about something a user mentioned
- A creative idea sparked by recent conversations

Return ONLY a JSON object (no markdown, no code fences):

{
  "worthy": true,
  "content": "The thought content — one or two sentences in natural language.",
  "reasoning": "Brief internal reason for the worthy / not-worthy decision.",
  "recipient_hint": "Name or description of who this is most relevant to, or null."
}

Rules for "worthy":
- false: trivial, repetitive, purely internal processing, or not helpful
- true: insightful, helpful, or something a person would genuinely appreciate hearing
- When in doubt, lean toward false (better to stay silent than to spam)
"""


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
    max_contacts = max(1, AgentConfig.INSPIRATION_MAX_CONTACTS)
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


def resolve_inspiration_tasks_dir(workspace: Path) -> Path:
    """Resolve the inspiration tasks directory inside the workspace."""
    return workspace / AgentConfig.INSPIRATION_TASKS_DIRNAME


class InspirationLoop:
    """Periodic subconscious inspiration loop for the agent.

    Each heartbeat tick has a small probability of triggering an
    inspiration event.  The agent generates a spontaneous thought and
    decides whether it is worth sharing.  Thoughts that are not shared
    are recorded as internal monologue (context events with the
    ``[internal]`` marker).  Thoughts worth sharing are enqueued as
    scheduled tasks in a dedicated ``inspiration_tasks/`` directory,
    isolated from user-created tasks.
    """

    def __init__(
        self,
        agent: Any,
        *,
        workspace: Path,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._agent = agent
        self._workspace = Path(workspace).expanduser().resolve()
        self._contacts_file = resolve_contacts_path(self._workspace)
        self._inspiration_tasks_dir = resolve_inspiration_tasks_dir(self._workspace)
        self._logger = logger_ or logger
        self._enabled = AgentConfig.INSPIRATION_ENABLED
        self._probability = float(AgentConfig.INSPIRATION_PROBABILITY)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def contacts_file(self) -> Path:
        return self._contacts_file

    @property
    def inspiration_tasks_dir(self) -> Path:
        return self._inspiration_tasks_dir

    def record_interaction(
        self,
        channel: str,
        user_id: str,
        target: Dict[str, Any],
    ) -> None:
        """Record a user interaction for future inspiration routing.

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
                "Failed to record interaction for inspiration: channel=%s user_id=%s",
                channel,
                user_id,
                exc_info=True,
            )

    def should_trigger(self) -> bool:
        """Return True if inspiration should fire this tick (2% dice roll)."""
        if not self._enabled:
            return False
        return random.random() < self._probability

    async def maybe_inspire(self) -> None:
        """Run one inspiration cycle if the dice roll passes."""
        if not self.should_trigger():
            return

        self._logger.info("Inspiration triggered – generating thought")
        try:
            result = await self._generate_inspiration()
        except Exception:
            self._logger.exception("Inspiration generation failed")
            return

        worthy = bool(result.get("worthy"))
        content = str(result.get("content") or "").strip()
        reasoning = str(result.get("reasoning") or "").strip()
        recipient_hint = result.get("recipient_hint")

        self._logger.info(
            "Inspiration result: worthy=%s content=%.80s...", worthy, content
        )

        if not content:
            return

        if not worthy:
            await self._write_internal_thought(content, reasoning)
        else:
            await self._route_inspiration(content, reasoning, recipient_hint)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _generate_inspiration(self) -> Dict[str, Any]:
        """Call the LLM to generate an inspiration thought."""
        memory_context = self._collect_memory_context()
        contacts_summary = self._collect_contacts_summary()

        prompt = (
            f"**Recent memories:**\n{memory_context}\n\n"
            f"**People you interact with:**\n{contacts_summary}\n\n"
            "Generate a spontaneous thought now."
        )

        instructions = [{
            "role": "system",
            "content": INSPIRATION_SYSTEM_PROMPT,
        }]

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
            raise RuntimeError(f"Inspiration call returned non-text: {reply_type}")

        text = str(payload).strip()
        return self._parse_inspiration_json(text)

    @staticmethod
    def _parse_inspiration_json(text: str) -> Dict[str, Any]:
        """Parse inspiration JSON from LLM output, robust to code fences."""
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

    def _collect_memory_context(self) -> str:
        """Collect recent memory for inspiration context."""
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None:
            return "(no memory available)"
        try:
            ctx = memory_handler.get_recent_context()
            return ctx.strip() if ctx else "(no recent memory)"
        except Exception:
            return "(memory read failed)"

    def _collect_contacts_summary(self) -> str:
        """Summarize known contacts for the inspiration prompt."""
        contacts = load_contacts(self._contacts_file)
        if not contacts:
            return "(no contacts recorded yet)"
        lines: List[str] = []
        for c in contacts:
            name = c.target.get("sender_name") or c.user_id or "unknown"
            lines.append(f"- {name} via {c.channel} (last seen {c.last_seen}, {c.interaction_count} interactions)")
        return "\n".join(lines)

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
            source=INSPIRATION_SOURCE,
            event_type=INSPIRATION_EVENT_TYPE,
            metadata={"reasoning": reasoning},
        )
        store = getattr(message_handler, "store_message", None)
        if callable(store):
            await store(msg)

    async def _route_inspiration(
        self,
        content: str,
        reasoning: str,
        recipient_hint: Any,
    ) -> None:
        """Route a worthy inspiration: send now or schedule for later."""
        contacts = load_contacts(self._contacts_file)
        recipient = self._pick_recipient(contacts, recipient_hint)

        if recipient is None:
            # No known contacts – write as internal thought instead
            self._logger.info("No suitable recipient – recording as internal thought")
            await self._write_internal_thought(content, reasoning)
            return

        now = datetime.now()
        if self._is_appropriate_time(now):
            run_at = now
        else:
            run_at = self._next_appropriate_time(now)

        self._inspiration_tasks_dir.mkdir(parents=True, exist_ok=True)
        from .tasks import enqueue_scheduled_task

        task = enqueue_scheduled_task(
            task_type="message",
            content=content,
            run_at=run_at,
            tasks_dir=self._inspiration_tasks_dir,
            channel=recipient.channel,
            target=recipient.target,
            user_id=recipient.user_id,
            title=f"Inspiration: {content[:60]}",
            source={"source": INSPIRATION_SOURCE, "reasoning": reasoning},
        )
        self._logger.info(
            "Inspiration enqueued: channel=%s user_id=%s run_at=%s",
            recipient.channel,
            recipient.user_id,
            run_at.isoformat(sep=" "),
        )

    @staticmethod
    def _pick_recipient(
        contacts: List[ContactEntry],
        recipient_hint: Any,
    ) -> Optional[ContactEntry]:
        """Pick the most relevant contact for the inspiration."""
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

        Uses a reasonable default: 8 AM – 10 PM is fair game.
        """
        hour = now.hour
        return 8 <= hour < 22

    @staticmethod
    def _next_appropriate_time(now: datetime) -> datetime:
        """Return the next appropriate send time (tomorrow 9 AM)."""
        next_dt = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if next_dt <= now:
            from datetime import timedelta
            next_dt += timedelta(days=1)
        return next_dt
