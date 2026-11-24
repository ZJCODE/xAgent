"""Periodic loop for autonomous agent thought generation."""

from __future__ import annotations

import asyncio
import inspect
import uuid
import json
import logging
import os
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
from ..providers import maintenance_reasoning_config
from .task_receipts import DeliveryRejectedError, DeliveryUncertainError, sync_task_directory as _fsync_directory

logger = logging.getLogger(__name__)


class SubconsciousOutputError(ValueError):
    """The model did not return the reflection protocol."""


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


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write *payload* as formatted JSON to *path* atomically with fsync."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)


def save_contacts(contacts_file: Path, contacts: List[ContactEntry]) -> None:
    """Persist contacts to the JSON registry (atomic write)."""
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
    _atomic_write_json(contacts_file, payload)


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
        incoming_target = {k: v for k, v in dict(target).items() if v is not None and v != ""}
        matched = False
        updated_contacts: List[ContactEntry] = []
        for c in contacts:
            if c.channel == channel and c.user_id == user_id:
                matched = True
                merged_target = {**c.target, **dict(target), **incoming_target}
                updated_contacts.append(ContactEntry(
                    channel=channel,
                    user_id=user_id,
                    target=merged_target,
                    last_seen=now_iso,
                    interaction_count=c.interaction_count + 1,
                ))
            else:
                updated_contacts.append(c)
        if not matched:
            updated_contacts.append(ContactEntry(
                channel=channel,
                user_id=user_id,
                target=dict(target),
                last_seen=now_iso,
                interaction_count=1,
            ))
        save_contacts(contacts_file, updated_contacts)


def resolve_contacts_path(workspace: Path) -> Path:
    """Resolve the contacts JSON file path inside the workspace."""
    return workspace / CONTACTS_FILENAME


async def persist_subconscious_delivery(
    agent: Any,
    delivery: SubconsciousDelivery,
    *,
    channel: str,
    recipient_id: str,
    content: Optional[str] = None,
) -> Any:
    """Persist a delivered subconscious message into the agent's message storage."""
    message_handler = getattr(agent, "message_handler", None)
    store_model_reply = getattr(message_handler, "store_model_reply", None)
    if not callable(store_model_reply):
        return None
    return await store_model_reply(
        delivery.content if content is None else content,
        getattr(agent, "_assistant_sender_id", "agent"),
        metadata={
            "subconscious": {
                "source": "subconscious",
                "created_at": delivery.created_at.isoformat(sep=" "),
                "recipient": {
                    "channel": delivery.recipient.channel,
                    "user_id": delivery.recipient.user_id,
                    "target": delivery.recipient.target,
                },
            }
        },
        channel=channel,
        recipient_id=recipient_id,
    )


class SubconsciousLoop:
    """Periodic subconscious thought loop for the agent.

    At a fixed interval, reflect once while the foreground is idle.
    Optional notes enter the journal window; optional messages go to the existing
    delivery sink. No persistent psychological or delivery-count state.
    """

    def __init__(
        self,
        agent: Any,
        *,
        workspace: Path,
        interval_seconds: Optional[float] = None,
        delivery_sink: Optional[Callable[[SubconsciousDelivery], Awaitable[None] | None]] = None,
        deliverable_channels: Optional[Iterable[str]] = None,
        contact_deliverable: Optional[Callable[[ContactEntry], bool]] = None,
        logger_: Optional[logging.Logger] = None,
    ) -> None:
        self._agent = agent
        self._workspace = Path(workspace).expanduser().resolve()
        self._contacts_file = resolve_contacts_path(self._workspace)
        self._delivery_sink = delivery_sink
        self._deliverable_channels = self._normalize_deliverable_channels(deliverable_channels)
        self._contact_deliverable = contact_deliverable
        self._logger = logger_ or logger
        self._enabled = AgentConfig.SUBCONSCIOUS_ENABLED
        self._interval = float(
            AgentConfig.SUBCONSCIOUS_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
        )
        self._next_think_at = time.monotonic() + self._interval
        self._thinking = False
        self._delivery_retries = SUBCONSCIOUS_DELIVERY_RETRIES
        self._delivery_retry_delay_seconds = SUBCONSCIOUS_DELIVERY_RETRY_DELAY_SECONDS
        self._source_ids: set[int] = set()
        self._last_private_note = ""

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def contacts_file(self) -> Path:
        return self._contacts_file

    def set_deliverable_channels(self, channels: Iterable[str]) -> None:
        self._deliverable_channels = self._normalize_deliverable_channels(channels)

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

    def _request_version(self) -> int:
        return getattr(getattr(self._agent, "inbox", None), "request_version", 0)

    async def maybe_think(self) -> None:
        """Reflect at a fixed interval; foreground requests always take priority."""
        if (not self._enabled or self._interval <= 0 or self._thinking
                or time.monotonic() < self._next_think_at or self._is_agent_busy()):
            return
        self._thinking = True
        self._next_think_at = time.monotonic() + self._interval
        version = self._request_version()
        generation, note_status, delivery_status = "failed", "none", "none"
        try:
            contacts = self._filter_deliverable_contacts(load_contacts(self._contacts_file))
            self._logger.info(
                "Subconscious recipients: total=%s weixin=%s",
                len(contacts), sum(c.channel == "weixin" for c in contacts),
            )
            result = await self._generate_subconscious_thought(contacts)
            generation = "ok"
            if self._is_agent_busy() or self._request_version() != version:
                generation = "interrupted"
                return
            note = result["note"]
            message = result["message"]
            # Send before diary I/O so a slow diary writer cannot stale the message.
            if message is not None:
                recipient = next((c for c in contacts if self._contact_key(c.channel, c.user_id)
                                  == message["recipient"]), None)
                if recipient is None:
                    delivery_status = "invalid_recipient"
                elif not self._filter_deliverable_contacts([recipient]):
                    delivery_status = "unavailable"
                else:
                    try:
                        delivery_status = await self._deliver_once(SubconsciousDelivery(
                            content=message["text"], recipient=recipient,
                            internal_content=note or "", created_at=datetime.now(),
                        ), version, message.get("source_id"))
                    except InterruptedError:
                        delivery_status = "interrupted"
                        return
                    except Exception:
                        delivery_status = "failed"
                        self._logger.warning("Subconscious delivery failed", exc_info=True)
            if note:
                note_status = await self._write_subconscious_thought(note)
        except SubconsciousOutputError:
            generation = "invalid_output"
            self._logger.warning("Invalid subconscious output", exc_info=True)
        except Exception:
            self._logger.warning("Subconscious generation failed", exc_info=True)
        finally:
            self._thinking = False
            self._logger.info("Subconscious cycle: generation=%s note=%s delivery=%s",
                              generation, note_status, delivery_status)

    def _is_agent_busy(self) -> bool:
        """Return True if a foreground turn is currently running or queued."""
        inbox = getattr(self._agent, "inbox", None)
        if inbox is None:
            return False
        busy = getattr(inbox, "busy", False)
        if isinstance(busy, bool) and busy:
            return True
        pending = getattr(inbox, "pending_count", 0)
        if isinstance(pending, int) and not isinstance(pending, bool) and pending > 0:
            return True
        return False

    async def _generate_subconscious_thought(self, contacts: List[ContactEntry]) -> Dict[str, Any]:
        """Run a subconscious agent turn and parse the final JSON result."""
        instructions, input_messages = await self._build_subconscious_turn_context(contacts)

        model_client = getattr(self._agent, "model_client", None)
        if model_client is None:
            raise RuntimeError("Agent has no model_client")
        if not callable(getattr(model_client, "model_turn_events", None)):
            raise RuntimeError("Agent model_client does not support model_turn_events()")

        text_parts: List[str] = []
        tool_calls = []
        async for model_event in model_client.model_turn_events(
            messages=input_messages,
            tool_specs=[],
            instructions=instructions,
            stream=False,
            max_tokens=AgentConfig.SUBCONSCIOUS_MAX_TOKENS,
            reasoning=maintenance_reasoning_config(
                getattr(model_client, "reasoning", None),
                provider_name=getattr(model_client, "provider_name", None),
                model=getattr(model_client, "model", None),
            ),
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

    async def _build_subconscious_turn_context(self, contacts: List[ContactEntry]) -> tuple[list[dict], list[dict]]:
        """Build model input using the same layers as a normal agent turn."""
        message_handler = getattr(self._agent, "message_handler", None)
        if message_handler is None:
            raise RuntimeError("Agent has no message_handler")

        working_context = await self._collect_working_context()
        hot_window = getattr(self._agent, "recent_messages", AgentConfig.DEFAULT_RECENT_MESSAGES)
        recent_messages = await message_handler.get_recent_messages(
            limit=AgentConfig.history_fetch_depth(hot_window)
        )
        memory_context = await self._collect_memory_context()
        relationship_context = await self._collect_relationship_context(contacts, recent_messages)
        notebook_query = self._extract_notebook_recall_query(memory_context, recent_messages)
        notebook_context = await self._collect_notebook_context(notebook_query)

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
            notebook_context=notebook_context,
            max_messages=hot_window,
            include_images=False,
            workspace_dir=getattr(self._agent, "workspace_dir", None),
            task_mode="subconscious_json",
            current_time=datetime.now().astimezone().isoformat(timespec="minutes"),
            working_summary=working_context.summary,
            covers_through_cursor=working_context.covers_through_cursor,
            prompt_registry=getattr(message_handler, "prompt_registry", None),
        )
        sources = []
        self._source_ids.clear()
        for msg in recent_messages[-hot_window:]:
            metadata = getattr(msg, "metadata", None) or {}
            source = metadata.get("source") or (
                "subconscious" if metadata.get("subconscious") else "unknown"
            )
            cursor = metadata.get(AgentConfig.MESSAGE_STORAGE_CURSOR_KEY)
            if type(cursor) is int and getattr(getattr(msg, "role", None), "value", None) in {"environment", "user"}:
                self._source_ids.add(cursor)
            sources.append({
                "id": cursor,
                "excerpt": str(getattr(msg, "content", "") or "")[:120],
                "timestamp": getattr(msg, "timestamp", None),
                "sender": getattr(msg, "sender_id", None),
                "channel": getattr(msg, "channel", None),
                "source": source,
            })
        iteration_messages.append({"role": "user", "content":
            "Sources for recent conversation records (context, not new requests). "
            "scheduled_task marks automation, not fresh words from a person. "
            "unknown means the original source is unverified.\n" +
            json.dumps(sources, ensure_ascii=False)})
        recipients = [{"recipient": self._contact_key(c.channel, c.user_id),
                       "name": c.target.get("sender_name") or c.target.get("display_name") or c.user_id}
                      for c in contacts]
        iteration_messages.append({"role": "user", "content":
            "Available recipients are listed below. If you send a message, use the exact identifier "
            "for the person you mean. If they are absent, leave the message field null.\n" +
            json.dumps(recipients, ensure_ascii=False)})
        input_messages = message_handler.sanitize_input_messages(list(iteration_messages))
        return instructions, input_messages

    async def _collect_working_context(self):
        """Collect rolling working context summary if the agent provides one."""
        from ..working_context import WorkingContextView

        getter = getattr(self._agent, "_working_context_for_turn", None)
        if callable(getter):
            try:
                view = getter()
                if inspect.isawaitable(view):
                    view = await view
                if isinstance(view, WorkingContextView):
                    return view
            except Exception:
                self._logger.warning("Failed to collect working context for subconscious", exc_info=True)
        return WorkingContextView()

    @staticmethod
    def _extract_notebook_recall_query(
        memory_context: str = "",
        recent_messages: Optional[List[Any]] = None,
    ) -> str:
        """Build a focused query for notebook recall from recent daily diary and messages.

        Strips longer-range yearly/monthly/weekly summaries when a recent daily
        diary section is present so multi-year summary vocabulary does not swamp
        keyword recall for current topics.
        """
        raw_memory = str(memory_context or "").strip()
        marker = "Recent daily diary:\n"
        if marker in raw_memory:
            daily_part = raw_memory.split(marker, 1)[1].strip()
        elif raw_memory.startswith("Longer-range diary summaries:\n"):
            daily_part = ""
        elif raw_memory in {"(no memory available)", "(no recent memory)", "(memory read failed)"}:
            daily_part = ""
        else:
            daily_part = raw_memory

        message_snippets: list[str] = []
        if isinstance(recent_messages, list):
            for msg in recent_messages[-6:]:
                content = getattr(msg, "content", None)
                if isinstance(content, str) and content.strip():
                    message_snippets.append(content.strip())

        parts = [part for part in (daily_part, "\n".join(message_snippets)) if part]
        if parts:
            return "\n\n".join(parts)
        return raw_memory

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
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise SubconsciousOutputError("Invalid subconscious JSON") from exc
        if not isinstance(parsed, dict) or set(parsed) != {"note", "message"}:
            raise SubconsciousOutputError("Expected note and message fields")
        note, message = parsed["note"], parsed["message"]
        if note is not None and not isinstance(note, str):
            raise SubconsciousOutputError("note must be text or null")
        if message is not None:
            if (not isinstance(message, dict) or set(message) - {"recipient", "text", "source_id"} or not {"recipient", "text"} <= set(message)
                    or any(not isinstance(message[k], str) or not message[k].strip()
                           for k in ("recipient", "text"))):
                raise SubconsciousOutputError("message must contain recipient and text")
            source_id = message.get("source_id")
            message = {k: message[k].strip() for k in ("recipient", "text")}
            if source_id is not None:
                if isinstance(source_id, str) and source_id.isascii() and source_id.isdecimal():
                    source_id = int(source_id)
                if type(source_id) is not int or source_id <= 0:
                    raise SubconsciousOutputError("Invalid result record ID")
                message["source_id"] = source_id
        return {"note": (note.strip() or None) if note else None, "message": message}

    @staticmethod
    def _looks_like_subconscious_payload(text: str) -> bool:
        """Keep both old and current protocol envelopes out of the diary."""
        lowered = str(text or "").lower()
        return (("internal_content" in lowered and
                 ("external_content" in lowered or "worthy" in lowered))
                or ('"note"' in lowered and '"message"' in lowered))

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

    async def _collect_notebook_context(self, memory_context: str = "") -> str:
        """Collect the notebook index as material for association.

        Recall is run against the recent diary rather than an incoming message,
        because a reflection turn has no speaker — what the agent has been
        living through is the closest thing to a query it has.
        """
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None or not callable(
            getattr(memory_handler, "get_notebook_context", None)
        ):
            return ""
        try:
            ctx = memory_handler.get_notebook_context(current_text=memory_context or "")
            if inspect.isawaitable(ctx):
                ctx = await ctx
        except Exception:
            self._logger.warning("Failed to collect notebook context", exc_info=True)
            return ""
        # The handler is duck-typed here; anything but text would be stringified
        # into the prompt.
        return ctx.strip() if isinstance(ctx, str) else ""

    async def _collect_relationship_context(self, contacts: List[ContactEntry], recent_messages: List[Any]) -> str:
        handler = getattr(self._agent, "memory_handler", None)
        if not callable(getattr(handler, "get_relationship_context", None)):
            return ""
        from ..handlers.memory import MemoryHandler

        people = MemoryHandler._extract_participants(list(reversed(recent_messages)))
        keys = list(dict.fromkeys([p["key"] for p in people] +
                                  [self._contact_key(c.channel, c.user_id) for c in contacts]))
        if not keys:
            return ""
        try:
            return await handler.get_relationship_context(
                speaker_keys=keys,
                max_cards=AgentConfig.RELATIONSHIP_SUBCONSCIOUS_MAX_CARDS,
                include_routing_id=True,
            )
        except Exception:
            self._logger.warning("Failed to collect relationship context", exc_info=True)
            return ""

    async def _write_subconscious_thought(self, content: str) -> str:
        """Record an inner experience, skipping verbatim repeats."""
        note = str(content or "").strip()
        if not note or self._looks_like_subconscious_payload(note):
            if note:
                self._logger.warning("Refusing to write subconscious JSON payload to diary")
            return "skipped"
        normalized_note = " ".join(note.split())
        if normalized_note and normalized_note == self._last_private_note:
            self._logger.debug("Skipping duplicate subconscious diary note")
            return "skipped"

        writer = getattr(self._agent, "record_subconscious_thought", None)
        if not callable(writer):
            return "skipped"
        try:
            res = writer(note)
            if inspect.isawaitable(res):
                await res
            self._last_private_note = normalized_note
            return "written"
        except Exception:
            self._logger.warning("Failed to record subconscious thought in diary", exc_info=True)
            return "failed"

    @staticmethod
    def _contact_key(channel: str, user_id: str) -> str:
        return f"{channel.strip().lower()}:{user_id.strip()}"

    async def _deliver_once(self, delivery, version, source_id):
        storage = getattr(self._agent, "message_storage", None)
        if not hasattr(storage, "reserve_proactive_send"):
            await self._deliver_with_retries(delivery, expected_version=version)
            return "sent"
        if source_id is not None and source_id not in self._source_ids:
            raise SubconsciousOutputError("Result record is outside the current context")
        recipient = self._contact_key(delivery.recipient.channel, delivery.recipient.user_id)
        source = str(source_id) if source_id is not None else uuid.uuid4().hex
        if not await asyncio.to_thread(storage.reserve_proactive_send, recipient, source):
            return "already_attempted"
        try:
            await self._deliver_with_retries(delivery, expected_version=version)
        except (ValueError, DeliveryRejectedError, InterruptedError):
            await asyncio.to_thread(storage.finish_proactive_send, recipient, source, "rejected")
            raise
        except Exception:
            await asyncio.to_thread(storage.finish_proactive_send, recipient, source, "unknown")
            raise
        await asyncio.to_thread(storage.finish_proactive_send, recipient, source, "accepted")
        return "sent"

    async def _deliver_with_retries(self, delivery: SubconsciousDelivery, *, expected_version: int) -> None:
        """Deliver a subconscious thought, retrying transient sink failures."""
        if self._delivery_sink is None:
            raise RuntimeError("Subconscious delivery sink is not configured")

        attempts = max(1, int(self._delivery_retries) + 1)
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            if self._is_agent_busy() or self._request_version() != expected_version:
                raise InterruptedError("Foreground request interrupted subconscious delivery")
            try:
                result = self._delivery_sink(delivery)
                if inspect.isawaitable(result):
                    await result
                return
            except (ValueError, DeliveryUncertainError):
                raise
            except Exception as exc:
                if isinstance(exc, DeliveryRejectedError) and not exc.retryable:
                    raise
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
    def _normalize_deliverable_channels(channels: Optional[Iterable[str]]) -> set[str]:
        if channels is None:
            return set()
        return {str(channel).strip().lower() for channel in channels if str(channel).strip()}

    def _filter_deliverable_contacts(self, contacts: List[ContactEntry]) -> List[ContactEntry]:
        filtered: List[ContactEntry] = []
        for contact in contacts:
            if str(contact.channel or "").strip().lower() not in self._deliverable_channels:
                continue
            if self._contact_deliverable is not None:
                try:
                    if not self._contact_deliverable(contact):
                        continue
                except Exception:
                    self._logger.debug(
                        "contact_deliverable check failed for %s:%s",
                        contact.channel,
                        contact.user_id,
                        exc_info=True,
                    )
                    continue
            filtered.append(contact)
        return filtered
