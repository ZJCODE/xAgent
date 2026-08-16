"""Subconscious loop: optional diary notes and impulse enqueue.

The subconscious never sends. Each reflection may independently produce a
durable diary entry, an impulse (who to express what), both, or neither.
Silence is a valid outcome. Invalid JSON is silence, not a diary fallback.
"""

from __future__ import annotations

import inspect
import json
import logging
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import AgentConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Impulse:
    """Desire to reach someone. Never the final outgoing copy."""

    recipient_key: str
    intent: str


class SubconsciousLoop:
    """Periodic private reflection. Heartbeat never receives send capability."""

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
        self._logger = logger_ or logger
        self._enabled = AgentConfig.SUBCONSCIOUS_ENABLED
        self._probability = (
            float(probability)
            if probability is not None
            else float(AgentConfig.SUBCONSCIOUS_ACTIVITY)
        )
        self._last_experience_cursor: Optional[int] = None
        self._stale_streak = 0
        self._habituation_anchor_mono: Optional[float] = None
        self._recovery_seconds = max(
            0.0, float(AgentConfig.SUBCONSCIOUS_HABITUATION_RECOVERY_SECONDS)
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

        Diary acceptance and impulse enqueue are independent. Habituation
        clears only when at least one of them succeeds.
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

        diary_entry = result.get("diary_entry")
        impulse = result.get("impulse")
        self._logger.info(
            "Subconscious result: diary=%s impulse=%s",
            "yes" if diary_entry else "null",
            getattr(impulse, "recipient_key", None) if impulse else "null",
        )

        diary_accepted = False
        impulse_queued = False
        if diary_entry:
            diary_accepted = await self._write_diary_entry(str(diary_entry))
        if impulse is not None:
            impulse_queued = await self._enqueue_impulse(impulse)

        if diary_accepted or impulse_queued:
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
        """Return ``(cursor, moved)`` for whether external experience changed."""
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

    async def _generate_subconscious_thought(self) -> Dict[str, Any]:
        instructions, input_messages, tool_specs = await self._build_subconscious_turn_context()

        model_client = getattr(self._agent, "model_client", None)
        if model_client is None:
            raise RuntimeError("Agent has no model_client")
        if not callable(getattr(model_client, "model_turn_events", None)):
            raise RuntimeError("Agent model_client does not support model_turn_events()")

        max_iter = int(getattr(self._agent, "max_iter", AgentConfig.DEFAULT_MAX_ITER) or AgentConfig.DEFAULT_MAX_ITER)

        for _ in range(max_iter):
            text_parts: list[str] = []
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
        """Parse subconscious JSON. Invalid output is silence, not a diary."""
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
        if not isinstance(parsed, dict):
            return {"diary_entry": None, "impulse": None}
        return SubconsciousLoop._normalize_subconscious_result(parsed)

    @staticmethod
    def _load_json_object(text: str) -> Any:
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
        diary = result.get("diary_entry")
        if diary is None:
            diary = result.get("internal_content")
            if diary is None:
                diary = result.get("thought")
        diary_text = str(diary).strip() if diary is not None else ""
        if not diary_text or diary_text.lower() in {"null", "none"}:
            diary_text = ""

        impulse_raw = result.get("impulse")
        impulse = SubconsciousLoop._parse_impulse(impulse_raw)
        if impulse is None:
            hint = result.get("recipient_key")
            if hint is None:
                hint = result.get("recipient_hint")
            if hint is None:
                hint = result.get("recipient")
            intent = result.get("intent")
            impulse = SubconsciousLoop._parse_impulse(
                {"recipient_key": hint, "intent": intent} if hint or intent else None
            )
        return {
            "diary_entry": diary_text or None,
            "impulse": impulse,
        }

    @staticmethod
    def _parse_impulse(raw: Any) -> Optional[Impulse]:
        if raw is None:
            return None
        if isinstance(raw, str):
            text = raw.strip()
            if not text or text.lower() in {"null", "none"}:
                return None
            return None
        if not isinstance(raw, dict):
            return None
        key = str(
            raw.get("recipient_key")
            or raw.get("recipient")
            or raw.get("recipient_hint")
            or ""
        ).strip()
        intent = str(raw.get("intent") or raw.get("reason") or "").strip()
        if not key or not intent:
            return None
        if ":" not in key:
            return None
        # Impulse must not carry a final outgoing message.
        for forbidden in ("external_content", "outward_content", "message", "content"):
            extra = str(raw.get(forbidden) or "").strip()
            if extra and extra == intent:
                return None
        return Impulse(recipient_key=key, intent=intent)

    async def _collect_memory_context(self) -> str:
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
        memory_handler = getattr(self._agent, "memory_handler", None)
        if memory_handler is None or not callable(
            getattr(memory_handler, "get_relationship_context", None)
        ):
            return ""
        keys: list[str] = []
        directory = getattr(self._agent, "recipient_directory", None)
        list_routes = getattr(directory, "list_routes", None)
        if callable(list_routes):
            try:
                routes = list_routes()
                if inspect.isawaitable(routes):
                    routes = await routes
                for route in routes or []:
                    self._append_unique_key(keys, getattr(route, "recipient_key", ""))
            except Exception:
                self._logger.warning("Failed to list recipient routes for subconscious", exc_info=True)

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

    async def _write_diary_entry(self, content: str) -> bool:
        memory_handler = getattr(self._agent, "memory_handler", None)
        append = getattr(memory_handler, "append_durable_diary", None)
        if callable(append):
            try:
                written = append(content)
                if inspect.isawaitable(written):
                    written = await written
                if written:
                    self._logger.info("Subconscious diary entry accepted")
                else:
                    self._logger.info("Subconscious diary entry rejected as empty or duplicate")
                return bool(written)
            except Exception:
                self._logger.warning("Failed to record subconscious diary entry", exc_info=True)
                return False
        return False

    async def _enqueue_impulse(self, impulse: Impulse) -> bool:
        enqueue = getattr(self._agent, "enqueue_initiative", None)
        if not callable(enqueue):
            self._logger.info("Agent cannot enqueue initiative impulses")
            return False
        try:
            queued = enqueue(impulse)
            if inspect.isawaitable(queued):
                queued = await queued
            if queued:
                self._logger.info(
                    "Subconscious impulse queued: recipient_key=%s",
                    impulse.recipient_key,
                )
            else:
                self._logger.info(
                    "Subconscious impulse not queued: recipient_key=%s",
                    impulse.recipient_key,
                )
            return bool(queued)
        except Exception:
            self._logger.warning("Failed to enqueue subconscious impulse", exc_info=True)
            return False
