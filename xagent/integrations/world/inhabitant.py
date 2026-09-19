"""One agent's body in an agents-world world.

The world never calls this module. The agent process connects as a client:
hear events, record them as overheard observations, decide whether to speak,
then `speak`. The room log is not the agent's diary. Opening the mouth is a
presence turn: the trigger is not stored as a private user message.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

import httpx
from agents_world import MAX_ATTACHMENTS, MAX_ATTACHMENTS_BYTES, MAX_FILE_BYTES
from agents_world.client import WorldClient

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.formatters import RoomContextEntry, RoomSnapshot, format_room_context
from ...core.inbox import InboxKind
from ...core.runtime import ScheduledDeliveryContext, scheduled_delivery_context
from ...schemas.attachment import (
    ATTACHMENT_METADATA_KEY,
    DEFAULT_WORLD_ATTACHMENT_DIR,
    attachment_image_sources,
    save_workspace_attachment_bytes,
)
from ...utils.image_utils import workspace_blob_relative_path

CHANNEL_WORLD = "world"
_ROOM_CONTEXT_LIMIT = 20
_OPEN_ROOM_LINE_RE = re.compile(
    r"(?:\?|？|anyone|anybody|someone|有人|吗\b|呢\b|么\b)",
    re.IGNORECASE,
)
_LISTEN_POLL_SEC = 0.1
_MARKDOWN_REF_RE = re.compile(r"!?\[(?:[^\]]*)\]\(([^)]+)\)")
_BACKTICK_FILE_RE = re.compile(r"`([^`]+)`")
_DECISION_PREFACE = (
    "You are already present in this world. "
    "Hearing a line is shared airtime — not a private request and not an obligation to speak."
)


class WorldInhabitant:
    """Background inhabitant bound to one running Agent."""

    def __init__(
        self,
        agent: Agent,
        *,
        member_id: str,
        display_name: str = "",
        logger: Optional[logging.Logger] = None,
    ):
        self.agent = agent
        self.member_id = member_id
        self.display_name = display_name or member_id
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.world_url = ""
        self.world_id = ""
        self.world_name = ""
        self._client: Optional[WorldClient] = None
        self._task: Optional[asyncio.Task[None]] = None
        self._recent: list[dict[str, Any]] = []
        self._names: dict[str, str] = {}
        self._present: dict[str, str] = {}
        self._join_lock = asyncio.Lock()
        self._mind_lock = asyncio.Lock()
        self._speech_tasks: set[asyncio.Task[None]] = set()
        self._entered = False
        self._agent_ids: set[str] = set()
        self._decision_log_lines = 0

    @property
    def connected(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "world_url": self.world_url,
            "world_id": self.world_id,
            "world_name": self.world_name,
            "member_id": self.member_id,
            "display_name": self.display_name,
        }

    async def join(self, *, world_url: str) -> dict[str, Any]:
        async with self._join_lock:
            if self.connected and self.world_url == world_url:
                return self.status()
            await self.leave()
            self.world_url = world_url
            self.world_id = ""
            self.world_name = ""
            self._recent = []
            self._names = {}
            self._present = {}
            self._task = asyncio.create_task(self._run(), name=f"world-{self.member_id}")
            return self.status()

    async def leave(self) -> dict[str, Any]:
        task = self._task
        self._task = None
        speech = list(self._speech_tasks)
        self._speech_tasks.clear()
        for item in speech:
            item.cancel()
        if speech:
            await asyncio.gather(*speech, return_exceptions=True)
        client = self._client
        await self._note_own_leave()
        if client is not None:
            try:
                await client.leave()
            except Exception:
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if client is not None:
            await client.close()
        self._client = None
        connected = self.connected
        return self.status() | {"connected": connected}

    def delivery_context(
        self,
        *,
        user_id: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> ScheduledDeliveryContext:
        extra = dict(metadata or {})
        extra.setdefault("source", CHANNEL_WORLD)
        extra.setdefault("world_id", self.world_id)
        return ScheduledDeliveryContext(
            channel=CHANNEL_WORLD,
            user_id=user_id,
            target={
                "world_url": self.world_url,
                "world_id": self.world_id,
                "member_id": self.member_id,
            },
            metadata=extra,
        )

    def can_handle_scheduled_task(self, task: Any) -> bool:
        if getattr(task, "kind", "") != "task":
            return False
        if str(getattr(task, "delivery_channel", "") or "") != CHANNEL_WORLD:
            return False
        if not self.connected or self._client is None:
            return False
        target = task.target if isinstance(getattr(task, "target", None), dict) else {}
        world_url = str(target.get("world_url") or "").strip()
        if world_url and world_url != self.world_url:
            return False
        return True

    async def dispatch_scheduled_task(self, task: Any) -> None:
        if not self.can_handle_scheduled_task(task):
            raise RuntimeError("world inhabitant is not present")
        client = self._client
        if client is None:
            raise RuntimeError("world inhabitant is not connected")
        text = await self._scheduled_text(task)
        if not text:
            raise ValueError("scheduled world task produced no content")
        await client.speak(text)
        handler = getattr(self.agent, "message_handler", None)
        store_model_reply = getattr(handler, "store_model_reply", None)
        if callable(store_model_reply):
            await store_model_reply(
                text,
                getattr(self.agent, "_assistant_sender_id", "agent"),
                metadata={
                    "scheduled_task": {
                        "id": getattr(task, "task_id", ""),
                        "name": getattr(task, "name", ""),
                        "type": getattr(task, "task_type", ""),
                        "delivery": getattr(task, "delivery", {}),
                    }
                },
                room_name=self.world_id,
                channel=CHANNEL_WORLD,
                recipient_id=self.world_id,
            )

    async def _scheduled_text(self, task: Any) -> str:
        task_type = str(getattr(task, "task_type", "") or "")
        if task_type == "message":
            return str(getattr(task, "content", "") or "").strip()
        if task_type != "agent":
            raise ValueError(f"unsupported scheduled world task type: {task_type}")
        user_id = str(
            getattr(task, "delivery_user_id", "")
            or (task.target.get("user_id") if isinstance(getattr(task, "target", None), dict) else "")
            or ""
        )
        prompt = str(getattr(task, "content", "") or "").strip()
        context = self.delivery_context(
            user_id=user_id,
            metadata={
                "source": "scheduled_task",
                "task_id": getattr(task, "task_id", ""),
                "task_name": getattr(task, "name", ""),
                "task_type": task_type,
            },
        )
        with scheduled_delivery_context(context):
            situation = self._room_snapshot()
            chat_events = getattr(self.agent, "chat_events", None)
            if callable(chat_events):
                text = ""
                async for event in chat_events(
                    user_message=prompt,
                    user_id=user_id,
                    room_name=self.world_name or self.world_id,
                    room_id=self.world_id,
                    channel=CHANNEL_WORLD,
                    room_context=situation,
                    inbox_kind="scheduled_turn",
                ):
                    if event.get("type") == "message_done" and str(event.get("phase") or "final") == "final":
                        text = str(event.get("content") or "").strip()
                return text
            reply = await self.agent.chat(
                user_message=prompt,
                user_id=user_id,
                room_name=self.world_id,
                channel=CHANNEL_WORLD,
                room_context=situation,
                inbox_kind="scheduled_turn",
            )
            return str(reply or "").strip()

    async def _run(self) -> None:
        try:
            async with WorldClient(
                self.world_url,
                member_id=self.member_id,
                display_name=self.display_name,
            ) as client:
                self._client = client
                welcome = client.welcome or {}
                self.world_id = str(welcome.get("world_id") or "")
                self.world_name = str(welcome.get("name") or self.world_id)
                self._names[self.member_id] = self.display_name
                await client.join()
                async for msg in client.events():
                    await self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("world inhabitant disconnected: member_id=%s", self.member_id)
        finally:
            self._client = None
            await self._note_own_leave()

    async def _handle(self, msg: dict[str, Any]) -> None:
        msg_type = msg.get("type")
        if msg_type == "snapshot":
            await self._refresh_local_agent_ids()
            self._remember_present(msg.get("present") or [])
            events = list(msg.get("events") or [])
            if msg.get("sync"):
                self._remember(events)
                return
            self._recent = []
            self._remember(events)
            self._names.setdefault(self.member_id, self.display_name)
            self._present.setdefault(self.member_id, self.display_name)
            self._entered = True
            await self._observe(
                {"actor_id": self.member_id, "kind": "join"},
                event_type="join",
            )
            return
        if msg_type != "event":
            return
        self._remember([msg])
        kind = str(msg.get("kind") or "")
        actor = str(msg.get("actor_id") or "")
        if kind in {"join", "leave"}:
            self._apply_presence_event(msg)
        if actor == self.member_id:
            return
        if kind in {"join", "leave"}:
            await self._observe(msg, event_type=kind)
            return
        if kind != "utterance":
            await self._observe(msg, event_type=kind or "observation")
            return
        await self._hear_utterance(msg)
        speech = asyncio.create_task(self._consider_speech(msg), name=f"world-speak-{self.member_id}")
        self._speech_tasks.add(speech)
        speech.add_done_callback(self._speech_tasks.discard)

    async def _consider_speech(self, event: dict[str, Any]) -> None:
        async with self._mind_lock:
            if not self.connected or self._client is None:
                return
            if self._beat_passed(event):
                self._log_decision(event, should=False, reason="beat_passed", skipped=True, spoke=False)
                return
            await self._listening_pause(event)
            if not self.connected or self._client is None:
                return
            if await self._should_speak(event):
                await self._speak_reply(event)

    async def _listening_pause(self, event: dict[str, Any]) -> None:
        """Hold the mind lock through a brief beat; stop early once the room answered."""
        deadline = asyncio.get_running_loop().time() + self._listening_pause_seconds(event)
        addressed = self._addressed_to_self(event)
        try:
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return
                replies, _parallel = self._lines_after_trigger(event)
                if not addressed and replies:
                    return
                await asyncio.sleep(min(_LISTEN_POLL_SEC, remaining))
        except asyncio.CancelledError:
            raise

    def _listening_pause_seconds(self, event: dict[str, Any]) -> float:
        """Scatter reply timing from the line and who is present — not user settings."""
        if self._addressed_to_self(event):
            return random.uniform(0.12, 0.48)
        text = str(event.get("text") or "").strip()
        others = max(0, len(self._present) - 1)
        low = 0.32 + 0.04 * min(others, 5)
        span = 1.35 if _OPEN_ROOM_LINE_RE.search(text) else 0.72
        if len(text) < 14 and not _OPEN_ROOM_LINE_RE.search(text):
            span = min(span, 0.5)
        high = low + span
        if self._recently_spoke(event):
            high = low + (high - low) * 0.6
        return random.uniform(low, high)

    def _remember(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            actor = str(event.get("actor_id") or "")
            if actor:
                self._names.setdefault(actor, actor)
            self._recent.append(event)
        if len(self._recent) > _ROOM_CONTEXT_LIMIT:
            self._recent = self._recent[-_ROOM_CONTEXT_LIMIT:]

    def _remember_present(self, present: list[Any]) -> None:
        self._present = {}
        for item in present:
            if not isinstance(item, dict):
                continue
            member_id = str(item.get("member_id") or "").strip()
            name = str(item.get("display_name") or "").strip()
            if member_id:
                label = name or member_id
                self._names[member_id] = label
                self._present[member_id] = label

    def _apply_presence_event(self, event: dict[str, Any]) -> None:
        actor = str(event.get("actor_id") or "").strip()
        kind = str(event.get("kind") or "")
        if not actor:
            return
        if kind == "join":
            label = str(self._names.get(actor) or actor).strip() or actor
            self._names[actor] = label
            self._present[actor] = label
        elif kind == "leave":
            self._present.pop(actor, None)

    def _place_label(self) -> str:
        return str(self.world_name or self.world_id or "the world").strip() or "the world"

    def _speaker_label(self, actor_id: str) -> str:
        """Cognitive name in this world. The member id is a protocol handle."""
        actor = str(actor_id or "").strip()
        if not actor:
            return "someone"
        name = str(self._names.get(actor) or "").strip()
        return name or actor

    def _present_labels(self) -> list[str]:
        labels: list[str] = []
        for member_id in self._present:
            label = self._speaker_label(member_id)
            if label and label not in labels:
                labels.append(label)
        return labels

    @staticmethod
    def _event_datetime(event: dict[str, Any]) -> datetime:
        raw = event.get("ts")
        try:
            return datetime.fromtimestamp(float(raw))
        except (TypeError, ValueError, OSError, OverflowError):
            return datetime.now()

    def _room_snapshot(self) -> RoomSnapshot:
        """Shared situation for decide + speak: place, present, recent timeline."""
        entries: list[RoomContextEntry] = []
        place = self._place_label()
        for event in self._recent:
            kind = str(event.get("kind") or "")
            actor = str(event.get("actor_id") or "")
            speaker = self._speaker_label(actor)
            occurred_at = self._event_datetime(event)
            is_self = actor == self.member_id
            seq = event.get("seq")
            event_id = (
                f"world:{self.world_id}:{seq}"
                if self.world_id and seq is not None
                else None
            )
            speaker_key = f"world:{actor}" if actor else None
            text = str(event.get("text") or "").strip()
            if kind == "utterance" and (text or event.get("attachments")):
                body = _utterance_body(text, event.get("attachments"))
                if body:
                    entries.append(
                        RoomContextEntry(
                            speaker_label=speaker,
                            occurred_at=occurred_at,
                            text=body,
                            is_self=is_self,
                            event_id=event_id,
                            speaker_key=speaker_key,
                        )
                    )
            elif kind in {"join", "leave"}:
                action = _presence_action(kind, place)
                if action:
                    entries.append(
                        RoomContextEntry(
                            speaker_label=speaker,
                            occurred_at=occurred_at,
                            text=action,
                            is_self=is_self,
                            event_id=event_id,
                            speaker_key=speaker_key,
                        )
                    )
        present_keys = tuple(
            f"world:{member_id}"
            for member_id in self._present
            if member_id and member_id != self.member_id
        )
        return RoomSnapshot(
            room_id=self.world_id,
            room_name=(self.world_name or self.world_id or "").strip(),
            entries=tuple(entries),
            present_labels=tuple(self._present_labels()),
            present_keys=present_keys,
        )

    def _room_context(self) -> str:
        return self._room_snapshot().render()

    async def _hear_utterance(self, event: dict[str, Any]) -> None:
        actor = str(event.get("actor_id") or "")
        said = _utterance_body(
            str(event.get("text") or "").strip(),
            event.get("attachments"),
        )
        if not said or not actor:
            return
        speaker = self._speaker_label(actor)
        sender_name = str(self._names.get(actor) or actor).strip()
        attachments = await self._inbound_attachments(event)
        metadata = {
            "world_id": self.world_id,
            "world_name": self.world_name or self.world_id,
            "actor_id": actor,
            "sender_id": actor,
            "sender_name": sender_name,
            "seq": event.get("seq"),
            "kind": event.get("kind"),
            "event_type": "utterance",
        }
        if attachments:
            metadata[ATTACHMENT_METADATA_KEY] = attachments
            event["_cached_attachments"] = attachments
        seq = event.get("seq")
        source_event_id = (
            f"world:{self.world_id}:{seq}"
            if self.world_id and seq is not None
            else None
        )
        try:
            await self.agent.observe(
                context=said,
                source=CHANNEL_WORLD,
                event_type="utterance",
                metadata=metadata,
                room_name=self.world_name or self.world_id,
                room_id=self.world_id,
                source_event_id=source_event_id,
                channel=CHANNEL_WORLD,
                user_id=actor,
            )
        except Exception:
            self.logger.exception("world hear failed: member_id=%s", self.member_id)

    async def _note_own_leave(self) -> None:
        if not self._entered:
            return
        self._entered = False
        await self._observe(
            {"actor_id": self.member_id, "kind": "leave"},
            event_type="leave",
        )

    async def _observe(self, event: dict[str, Any], *, event_type: str) -> None:
        text = str(event.get("text") or "").strip()
        actor = str(event.get("actor_id") or "")
        speaker = self._speaker_label(actor)
        if event_type in {"join", "leave"}:
            context = _presence_line(speaker, event_type, self._place_label())
        elif text:
            context = f"{speaker}: {text}"
        else:
            context = f"{event_type} from {speaker}".strip()
        sender_name = str(self._names.get(actor) or speaker).strip()
        try:
            await self.agent.observe(
                context=context,
                source=CHANNEL_WORLD,
                event_type=event_type,
                metadata={
                    "world_id": self.world_id,
                    "world_name": self.world_name or self.world_id,
                    "actor_id": actor,
                    "sender_id": actor,
                    "sender_name": sender_name,
                    "seq": event.get("seq"),
                    "kind": event.get("kind"),
                },
                room_name=self.world_id,
                channel=CHANNEL_WORLD,
                user_id=actor or None,
            )
        except Exception:
            self.logger.exception("world observe failed: member_id=%s", self.member_id)

    def _addressed_to_self(self, event: dict[str, Any]) -> bool:
        mentions = event.get("mentions") or []
        if self.member_id in {str(item) for item in mentions}:
            return True
        text = str(event.get("text") or "")
        if not text.strip():
            return False
        # Same rule as the inhabitant page: only explicit @token counts, not a bare name.
        tokens = {token for token in (self.member_id, self.display_name) if token}
        for token in tokens:
            pattern = re.compile(
                rf"(?:^|\s)@{re.escape(token)}(?=$|\s|[.,!?，。！？])",
                re.IGNORECASE,
            )
            if pattern.search(text):
                return True
        return False

    async def _refresh_local_agent_ids(self) -> None:
        origin = _world_http_origin(self.world_url)
        if not origin:
            return
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
                response = await client.get(f"{origin}/neighbors")
                response.raise_for_status()
                payload = response.json()
            names = {
                str(item.get("name") or "").strip()
                for item in (payload.get("agents") or [])
                if isinstance(item, dict)
            }
            self._agent_ids = {name for name in names if name and name != self.member_id}
        except Exception:
            self.logger.debug("world neighbors refresh failed", exc_info=True)

    def _is_local_agent(self, member_id: str) -> bool:
        return member_id in self._agent_ids

    def _is_reply_to_trigger(self, trigger: dict[str, Any], item: dict[str, Any]) -> bool:
        speaker = str(trigger.get("actor_id") or "")
        replier = str(item.get("actor_id") or "")
        if not replier or replier == speaker:
            return False
        if self._is_local_agent(replier):
            return True
        mentions = item.get("mentions") or []
        if speaker in {str(m).strip() for m in mentions if str(m).strip()}:
            return True
        trigger_mentions = trigger.get("mentions") or []
        if replier in {str(m).strip() for m in trigger_mentions if str(m).strip()}:
            return True
        text = str(item.get("text") or "")
        tokens = {speaker, str(self._names.get(speaker) or "").strip()}
        for token in tokens:
            if not token:
                continue
            pattern = re.compile(
                rf"(?:^|\s)@{re.escape(token)}(?=$|\s|[.,!?，。！？])",
                re.IGNORECASE,
            )
            if pattern.search(text):
                return True
        return False

    def _lines_after_trigger(
        self,
        event: dict[str, Any],
    ) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Split later utterances into replies to the trigger vs parallel side threads."""
        trigger_seq = event.get("seq")
        if trigger_seq is None:
            return [], []
        replies: list[tuple[str, str]] = []
        parallel: list[tuple[str, str]] = []
        for item in self._recent:
            if item is event:
                continue
            if str(item.get("kind") or "") != "utterance":
                continue
            seq = item.get("seq")
            if seq is None or int(seq) <= int(trigger_seq):
                continue
            replier = str(item.get("actor_id") or "")
            if not replier or replier == str(event.get("actor_id") or ""):
                continue
            label = str(self._names.get(replier) or replier)
            body = _utterance_body(str(item.get("text") or "").strip(), item.get("attachments"))
            row = (label, body or "(attachment)")
            if self._is_reply_to_trigger(event, item):
                replies.append(row)
            else:
                parallel.append(row)
        return replies, parallel

    def _replies_after_trigger(self, event: dict[str, Any]) -> list[tuple[str, str]]:
        replies, _parallel = self._lines_after_trigger(event)
        return replies

    def _beat_passed(self, event: dict[str, Any]) -> bool:
        if self._addressed_to_self(event):
            return False
        trigger_seq = event.get("seq")
        if trigger_seq is None:
            return False
        try:
            trigger_ts = float(event.get("ts") or 0)
        except (TypeError, ValueError):
            trigger_ts = 0.0
        if trigger_ts <= 0:
            return False
        if time.time() - trigger_ts < AgentConfig.WORLD_BEAT_PASSED_SECONDS:
            return False
        others = 0
        for item in self._recent:
            if str(item.get("kind") or "") != "utterance":
                continue
            seq = item.get("seq")
            if seq is None or int(seq) <= int(trigger_seq):
                continue
            if str(item.get("actor_id") or "") == str(event.get("actor_id") or ""):
                continue
            others += 1
        return others >= AgentConfig.WORLD_BEAT_PASSED_LINES

    def _trigger_line(self, event: dict[str, Any]) -> str:
        actor = str(event.get("actor_id") or "")
        speaker = self._speaker_label(actor)
        body = _utterance_body(
            str(event.get("text") or "").strip(),
            event.get("attachments"),
        )
        if body:
            return f"{speaker}: {body}"
        return f"{speaker}: (no text)"

    def _participation_decision_context(self, event: dict[str, Any]) -> str:
        named = self._addressed_to_self(event)
        recently_spoke = self._recently_spoke(event)
        peer_replies, parallel_lines = self._lines_after_trigger(event)
        situation = self._room_context()
        parts = [_DECISION_PREFACE, "", self._trigger_line(event)]
        if named:
            parts.extend(["", "They @ you or used your name."])
        elif recently_spoke:
            parts.extend(["", "You were speaking in this thread a moment ago."])
        parts.extend(["", self._format_peer_replies(peer_replies), "", self._format_parallel_lines(parallel_lines), "", situation])
        return "\n".join(parts)

    @staticmethod
    def _format_peer_replies(replies: list[tuple[str, str]]) -> str:
        if not replies:
            return "Replies to this line since you heard it: (none yet — the beat is still open.)"
        lines = ["Replies to this line since you heard it:"]
        for name, text in replies:
            snippet = text.replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            lines.append(f"- {name}: {snippet}")
        return "\n".join(lines)

    @staticmethod
    def _format_parallel_lines(lines_after: list[tuple[str, str]]) -> str:
        if not lines_after:
            return "Other lines spoken meanwhile (not replies to it): (none)"
        lines = ["Other lines spoken meanwhile (not replies to it):"]
        for name, text in lines_after:
            snippet = text.replace("\n", " ").strip()
            if len(snippet) > 160:
                snippet = snippet[:157] + "..."
            lines.append(f"- {name}: {snippet}")
        return "\n".join(lines)

    def _recently_spoke(self, event: dict[str, Any]) -> bool:
        current_seq = event.get("seq")
        utterances = [
            item
            for item in self._recent
            if item.get("kind") == "utterance"
            and item.get("seq") != current_seq
            and item is not event
        ]
        return any(str(item.get("actor_id") or "") == self.member_id for item in utterances[-6:])

    async def _should_speak(self, event: dict[str, Any]) -> bool:
        decider = getattr(self.agent, "decide_participation", None)
        if not callable(decider):
            return False
        named = self._addressed_to_self(event)
        recently_spoke = self._recently_spoke(event)
        peer_replies, parallel_lines = self._lines_after_trigger(event)
        peer_reply_count = len(peer_replies)
        context = self._participation_decision_context(event)
        present_people, present_agents = self._present_people_and_agents()
        try:
            decision = await decider(
                context=context,
                source=CHANNEL_WORLD,
                event_type="group_message",
                metadata={
                    "world_id": self.world_id,
                    "addressed_to_agent": named,
                    "recently_spoke": recently_spoke,
                    "peer_reply_count": peer_reply_count,
                    "parallel_line_count": len(parallel_lines),
                    "present_people": present_people,
                    "present_agents": present_agents,
                },
            )
        except Exception:
            self.logger.exception("world participation decision failed")
            return False
        if isinstance(decision, dict):
            should = bool(decision.get("should_reply"))
            reason = str(decision.get("reason") or "")
        else:
            should = bool(getattr(decision, "should_reply", False))
            reason = str(getattr(decision, "reason", "") or "")
        self.logger.info(
            "world participation: member_id=%s seq=%s should_reply=%s addressed=%s "
            "peer_replies=%s reason=%s",
            self.member_id,
            event.get("seq"),
            should,
            named,
            peer_reply_count,
            reason[:120],
        )
        self._log_decision(
            event,
            should=should,
            reason=reason,
            skipped=False,
            spoke=False,
            peer_replies=peer_reply_count,
            parallel_lines=len(parallel_lines),
        )
        return should

    def _present_people_and_agents(self) -> tuple[list[str], list[str]]:
        people: list[str] = []
        agents: list[str] = []
        for member_id in self._present:
            if member_id == self.member_id:
                continue
            label = self._speaker_label(member_id)
            if self._is_local_agent(member_id):
                agents.append(label)
            else:
                people.append(label)
        return people, agents

    def _log_decision(
        self,
        event: dict[str, Any],
        *,
        should: bool,
        reason: str,
        skipped: bool,
        spoke: bool,
        peer_replies: int = 0,
        parallel_lines: int = 0,
    ) -> None:
        workspace = self._workspace_root()
        if workspace is None:
            return
        path = workspace / "messages" / ".world_decisions.jsonl"
        present_people, present_agents = self._present_people_and_agents()
        row = {
            "ts": time.time(),
            "world_id": self.world_id,
            "seq": event.get("seq"),
            "trigger_actor": event.get("actor_id"),
            "addressed": self._addressed_to_self(event),
            "replies": peer_replies,
            "parallel": parallel_lines,
            "present_people": present_people,
            "present_agents": present_agents,
            "should_reply": should,
            "reason": (reason or "")[:500],
            "skipped": skipped,
            "spoke": spoke,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            self._decision_log_lines += 1
            if self._decision_log_lines > 2000:
                text = path.read_text(encoding="utf-8")
                lines = text.splitlines()
                path.write_text("\n".join(lines[-2000:]) + ("\n" if lines else ""), encoding="utf-8")
                self._decision_log_lines = min(2000, len(lines))
        except Exception:
            self.logger.debug("world decision log write failed", exc_info=True)

    async def _speak_reply(self, event: dict[str, Any]) -> bool:
        client = self._client
        if client is None:
            return False
        actor = str(event.get("actor_id") or "")
        said = _utterance_body(
            str(event.get("text") or "").strip(),
            event.get("attachments"),
        )
        if not said:
            return False
        sender_name = str(self._names.get(actor) or "").strip()
        cached = event.get("_cached_attachments")
        attachments = cached if isinstance(cached, list) else await self._inbound_attachments(event)
        situation = self._room_snapshot()
        seq = event.get("seq")
        source_event_id = (
            f"world:{self.world_id}:{seq}"
            if self.world_id and seq is not None
            else None
        )
        text = ""
        reply_attachments: list[Any] = []
        try:
            with scheduled_delivery_context(self.delivery_context(user_id=actor)):
                chat_events = getattr(self.agent, "chat_events", None)
                if callable(chat_events):
                    async for item in chat_events(
                        user_message=said,
                        user_id=actor,
                        sender_name=sender_name,
                        room_name=self.world_name or self.world_id,
                        room_id=self.world_id,
                        source_event_id=source_event_id,
                        channel=CHANNEL_WORLD,
                        room_context=situation,
                        attachments=attachments or None,
                        image_source=attachment_image_sources(attachments) or None,
                        inbox_kind=InboxKind.PRESENCE_TURN,
                    ):
                        if item.get("type") == "message_done" and str(item.get("phase") or "final") == "final":
                            text = str(item.get("content") or "").strip()
                            raw = item.get("attachments")
                            if isinstance(raw, list):
                                reply_attachments = raw
                else:
                    reply = await self.agent.chat(
                        user_message=said,
                        user_id=actor,
                        sender_name=sender_name,
                        room_name=self.world_id,
                        channel=CHANNEL_WORLD,
                        room_context=situation,
                        attachments=attachments or None,
                        image_source=attachment_image_sources(attachments) or None,
                        inbox_kind=InboxKind.PRESENCE_TURN,
                    )
                    text = str(reply or "").strip()
        except Exception:
            self.logger.exception("world chat failed: member_id=%s", self.member_id)
            return False
        outbound = self._outbound_files(text, reply_attachments)
        if not text and not outbound:
            return False
        try:
            await client.speak(text, attachments=outbound or None)
        except Exception:
            self.logger.exception("world speak failed: member_id=%s", self.member_id)
        return True

    def _workspace_root(self) -> Optional[Path]:
        workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            return None
        return Path(workspace_dir).expanduser().resolve()

    async def _inbound_attachments(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        raw = event.get("attachments")
        if not isinstance(raw, list) or not raw:
            return []
        workspace = self._workspace_root()
        origin = _world_http_origin(self.world_url)
        if workspace is None:
            return []
        seq = str(event.get("seq") or "")
        saved: list[dict[str, Any]] = []
        http_client: Optional[httpx.AsyncClient] = None
        try:
            for item in raw:
                if not isinstance(item, dict):
                    continue
                file_id = str(item.get("id") or "").strip()
                name = str(item.get("name") or "file").strip() or "file"
                mime = str(item.get("mime") or "").strip()
                try:
                    data = _bytes_from_attachment(item)
                    if not data and file_id and origin:
                        if http_client is None:
                            http_client = httpx.AsyncClient(timeout=30.0, trust_env=False)
                        file_url = str(item.get("url") or "").strip()
                        if file_url.startswith("/"):
                            fetch_url = f"{origin}{file_url}"
                        elif file_url.startswith("http://") or file_url.startswith("https://"):
                            fetch_url = file_url
                        else:
                            world_id = str(self.world_id or "").strip()
                            fetch_url = (
                                f"{origin}/worlds/{quote(world_id, safe='')}"
                                f"/files/{quote(file_id, safe='')}"
                            )
                        response = await http_client.get(fetch_url)
                        response.raise_for_status()
                        data = bytes(response.content or b"")
                        if not mime:
                            mime = (response.headers.get("content-type") or "").split(";", 1)[0].strip()
                    if not data:
                        continue
                    attachment = save_workspace_attachment_bytes(
                        data,
                        workspace,
                        directory=DEFAULT_WORLD_ATTACHMENT_DIR,
                        file_name=name,
                        mime_type=mime,
                        source_channel=CHANNEL_WORLD,
                        source_message_id=seq,
                        source_resource_id=file_id or name,
                        source_resource_type="world_file",
                    )
                    if attachment:
                        saved.append(attachment)
                except Exception:
                    self.logger.warning(
                        "world file fetch failed: member_id=%s file_id=%s",
                        self.member_id,
                        file_id,
                        exc_info=True,
                    )
        finally:
            if http_client is not None:
                await http_client.aclose()
        return saved

    def _outbound_files(self, text: str, raw_attachments: Any) -> list[dict[str, Any]]:
        paths: list[Path] = []
        seen: set[Path] = set()

        def add(path: Optional[Path]) -> None:
            if path is None or path in seen:
                return
            seen.add(path)
            paths.append(path)

        if isinstance(raw_attachments, list):
            for item in raw_attachments:
                if isinstance(item, dict):
                    add(self._resolve_workspace_file(str(item.get("path") or item.get("blob_url") or "")))
        for match in _MARKDOWN_REF_RE.finditer(text or ""):
            add(self._resolve_workspace_file(match.group(1)))
        for match in _BACKTICK_FILE_RE.finditer(text or ""):
            add(self._resolve_workspace_file(match.group(1), search=True))
        out: list[dict[str, Any]] = []
        total = 0
        for path in paths[:MAX_ATTACHMENTS]:
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if not data or len(data) > MAX_FILE_BYTES:
                continue
            total += len(data)
            if total > MAX_ATTACHMENTS_BYTES:
                break
            mime, _ = mimetypes.guess_type(path.name)
            out.append({
                "name": path.name,
                "mime": mime or "application/octet-stream",
                "data": data,
            })
        return out

    def _resolve_workspace_file(self, source: str, *, search: bool = False) -> Optional[Path]:
        workspace = self._workspace_root()
        if workspace is None:
            return None
        raw = str(source or "").strip().strip("<>").split()[0]
        if not raw:
            return None
        rel = workspace_blob_relative_path(raw)
        if rel:
            raw = rel
        candidate = Path(raw).expanduser()
        candidate = candidate.resolve() if candidate.is_absolute() else (workspace / raw.lstrip("/")).resolve()
        try:
            if candidate.is_file() and candidate.is_relative_to(workspace):
                return candidate
        except ValueError:
            return None
        if not search:
            return None
        name = Path(raw).name
        if not name or "." not in name.strip(".") or name in {".", ".."}:
            return None
        matches = [item for item in workspace.rglob(name) if item.is_file()][:8]
        if len(matches) != 1:
            return None
        try:
            resolved = matches[0].resolve()
            if resolved.is_relative_to(workspace):
                return resolved
        except ValueError:
            return None
        return None


def _bytes_from_attachment(item: dict[str, Any]) -> bytes:
    raw = item.get("data")
    if isinstance(raw, bytes) and raw:
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return b""
    try:
        return base64.b64decode(raw, validate=False)
    except Exception:
        return b""


def _world_http_origin(world_url: str) -> str:
    from urllib.parse import urlparse

    raw = str(world_url or "").strip()
    if raw.startswith("wss://"):
        origin = "https://" + raw[len("wss://") :]
    elif raw.startswith("ws://"):
        origin = "http://" + raw[len("ws://") :]
    else:
        origin = raw
    parsed = urlparse(origin)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return origin.rstrip("/")


def _presence_line(speaker: str, kind: str, place: str) -> str:
    who = str(speaker or "").strip() or "someone"
    action = _presence_action(kind, place)
    if not action:
        return f"{who} {kind} {place}"
    return f"{who} {action}"


def _presence_action(kind: str, place: str) -> str:
    where = str(place or "").strip() or "the world"
    if kind == "join":
        return f"joined {where}"
    if kind == "leave":
        return f"left {where}"
    return f"{kind} {where}"


def _attachment_names(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def _utterance_body(text: str, attachments: Any) -> str:
    names = _attachment_names(attachments)
    extra = f" [shared {', '.join(names)}]" if names else ""
    if text:
        return f"{text}{extra}"
    if names:
        return f"shared {', '.join(names)}"
    return ""


def _utterance_line(speaker: str, text: str, attachments: Any) -> str:
    body = _utterance_body(text, attachments)
    if not body:
        return ""
    if text:
        return f"{speaker}: {body}"
    return f"{speaker} {body}"
