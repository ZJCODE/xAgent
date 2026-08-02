"""Rolling working-context summary for prompt continuity.

This is intentionally separate from diary memory:
- Stored under messages/, never under memory/
- Never written into the message SQLite stream
- Disposable and rebuildable; diary remains the only memory carrier
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, List, Optional, Protocol

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX platforms
    msvcrt = None

from ..schemas import Message, MessageType, RoleType
from .config import AgentConfig
from .journal import JournalLLMService

logger = logging.getLogger(__name__)


@dataclass
class WorkingContextState:
    covers_through_cursor: int = 0
    updated_at: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "covers_through_cursor": int(self.covers_through_cursor),
            "updated_at": float(self.updated_at),
            "summary": str(self.summary or ""),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkingContextState":
        try:
            covers = int(payload.get("covers_through_cursor", 0) or 0)
        except (TypeError, ValueError):
            covers = 0
        try:
            updated_at = float(payload.get("updated_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            updated_at = 0.0
        summary = str(payload.get("summary", "") or "").strip()
        return cls(
            covers_through_cursor=max(0, covers),
            updated_at=max(0.0, updated_at),
            summary=summary,
        )


@dataclass(frozen=True)
class WorkingContextView:
    """Prompt-facing snapshot after optional compaction."""

    summary: str = ""
    covers_through_cursor: int = 0


class WorkingContextStore:
    """File-backed single-slot working summary under the messages directory."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_name(AgentConfig.WORKING_CONTEXT_LOCK_FILENAME)

    def read(self) -> WorkingContextState:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return WorkingContextState()
        except OSError as exc:
            logger.warning("Failed to read working context: %s", exc)
            return WorkingContextState()
        if not raw:
            return WorkingContextState()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid working context JSON: %s", exc)
            return WorkingContextState()
        if not isinstance(payload, dict):
            return WorkingContextState()
        return WorkingContextState.from_dict(payload)

    def write(self, state: WorkingContextState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), ensure_ascii=False, indent=2)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(payload + "\n", encoding="utf-8")
        tmp_path.replace(self.path)

    def acquire_lock(self) -> IO[str]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.lock_path.open("a+", encoding="utf-8")
        try:
            self._lock_file(lock_file)
        except Exception:
            lock_file.close()
            raise
        return lock_file

    def release_lock(self, lock_file: IO[str]) -> None:
        try:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            lock_file.close()

    @staticmethod
    def _lock_file(lock_file: IO[str]) -> None:
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            return
        if msvcrt is not None:
            lock_file.seek(0)
            if not lock_file.read(1):
                lock_file.write("\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            return
        raise RuntimeError("No supported file locking implementation is available")


class WorkingContextSummarizer:
    """LLM summarizer for working context. Not diary prose."""

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        provider_name: str,
        model_api: str,
        reasoning: Any = None,
        max_tokens: int = AgentConfig.WORKING_CONTEXT_SUMMARY_MAX_TOKENS,
        summary_max_chars: int = AgentConfig.WORKING_CONTEXT_SUMMARY_MAX_CHARS,
    ) -> None:
        self._llm = JournalLLMService(
            client=client,
            model=model,
            provider_name=provider_name,
            model_api=model_api,
            max_tokens=max_tokens,
            reasoning=reasoning,
        )
        self.summary_max_chars = max(200, int(summary_max_chars))

    async def summarize(
        self,
        *,
        previous_summary: str,
        records: List[dict],
    ) -> str:
        transcript = JournalLLMService._format_transcript(records)
        if not transcript.strip() and not previous_summary.strip():
            return ""

        system_prompt = self.build_system_prompt()
        user_prompt = self.build_user_prompt(
            previous_summary=previous_summary,
            transcript=transcript,
            max_chars=self.summary_max_chars,
        )
        content = await self._llm._call_text(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
        )
        normalized = JournalLLMService._normalize_content(content)
        if len(normalized) > self.summary_max_chars:
            normalized = normalized[: self.summary_max_chars].rstrip() + "…"
        return normalized

    @staticmethod
    def build_system_prompt() -> str:
        return """Compress earlier conversation into a working context summary for an ongoing agent turn.

This is NOT a diary and NOT first-person life narrative.
Preserve speaker attribution, open commitments, unfinished tasks, key identifiers/paths, and decisions.
Omit feelings, style, and tool-output dumps unless a concrete fact is still needed.
Write concise bullet-like prose. Prefer the language of the conversation.
Return only the summary text."""

    @staticmethod
    def build_user_prompt(
        *,
        previous_summary: str,
        transcript: str,
        max_chars: int,
    ) -> str:
        previous = previous_summary.strip() or "(none)"
        body = transcript.strip() or "(no new messages)"
        return (
            f"Update the working context summary. Keep it under {max_chars} characters.\n\n"
            f"Previous working summary:\n{previous}\n\n"
            f"Newly rolled-off messages:\n{body}"
        )


class _MessageStorage(Protocol):
    async def get_latest_message_cursor(self) -> int: ...

    async def get_messages_in_cursor_range(
        self,
        start_exclusive: int = 0,
        end_inclusive: Optional[int] = None,
    ) -> List[Message]: ...


class WorkingContextCompactor:
    """Keep one rolling working summary covering everything before the hot window."""

    def __init__(
        self,
        *,
        store: WorkingContextStore,
        message_storage: _MessageStorage,
        summarizer: WorkingContextSummarizer,
        hot_window: int,
        roll_slack: int | None = None,
    ) -> None:
        self.store = store
        self.message_storage = message_storage
        self.summarizer = summarizer
        self.hot_window = max(1, int(hot_window))
        if roll_slack is None:
            self.roll_slack = AgentConfig.working_context_roll_slack(self.hot_window)
        else:
            self.roll_slack = max(0, int(roll_slack))
        self._lock = asyncio.Lock()

    async def ensure_fresh(self) -> WorkingContextView:
        """Roll summary if needed; return prompt-facing summary + coverage."""
        async with self._lock:
            lock_file = await asyncio.to_thread(self.store.acquire_lock)
            try:
                return await self._ensure_fresh_locked()
            finally:
                await asyncio.to_thread(self.store.release_lock, lock_file)

    async def current_view(self) -> WorkingContextView:
        state = await asyncio.to_thread(self.store.read)
        return WorkingContextView(
            summary=state.summary.strip(),
            covers_through_cursor=max(0, int(state.covers_through_cursor)),
        )

    async def current_summary(self) -> str:
        return (await self.current_view()).summary

    async def _ensure_fresh_locked(self) -> WorkingContextView:
        state = await asyncio.to_thread(self.store.read)
        try:
            latest = int(await self.message_storage.get_latest_message_cursor())
        except Exception as exc:
            logger.warning("Working context cursor read failed: %s", exc)
            return WorkingContextView(
                summary=state.summary.strip(),
                covers_through_cursor=max(0, int(state.covers_through_cursor)),
            )

        latest = max(0, latest)
        covers = max(0, int(state.covers_through_cursor))
        pending = latest - covers
        threshold = self.hot_window + self.roll_slack
        if pending <= threshold:
            return WorkingContextView(
                summary=state.summary.strip(),
                covers_through_cursor=covers,
            )

        roll_end = latest - self.hot_window
        if roll_end <= covers:
            return WorkingContextView(
                summary=state.summary.strip(),
                covers_through_cursor=covers,
            )

        try:
            messages = await self.message_storage.get_messages_in_cursor_range(
                start_exclusive=covers,
                end_inclusive=roll_end,
            )
        except Exception as exc:
            logger.warning("Working context message load failed: %s", exc)
            return WorkingContextView(
                summary=state.summary.strip(),
                covers_through_cursor=covers,
            )

        records = [
            self._experience_record(message)
            for message in messages
            if self._is_summarizable(message)
        ]
        if not records and not state.summary.strip():
            # Advance coverage even when the rolled window had nothing useful,
            # so we do not keep re-scanning empty gaps.
            new_state = WorkingContextState(
                covers_through_cursor=roll_end,
                updated_at=time.time(),
                summary=state.summary,
            )
            await asyncio.to_thread(self.store.write, new_state)
            return WorkingContextView(summary="", covers_through_cursor=roll_end)

        try:
            summary = await self.summarizer.summarize(
                previous_summary=state.summary,
                records=records,
            )
        except Exception as exc:
            logger.warning("Working context summarize failed: %s", exc)
            return WorkingContextView(
                summary=state.summary.strip(),
                covers_through_cursor=covers,
            )

        new_state = WorkingContextState(
            covers_through_cursor=roll_end,
            updated_at=time.time(),
            summary=summary.strip(),
        )
        await asyncio.to_thread(self.store.write, new_state)
        return WorkingContextView(
            summary=new_state.summary,
            covers_through_cursor=roll_end,
        )

    @staticmethod
    def _experience_record(message: Message) -> dict:
        return {
            "role": message.role.value,
            "type": message.type.value,
            "sender_id": message.sender_id,
            "content": message.content,
            "timestamp": message.timestamp,
            "channel": message.channel,
            "room_name": message.room_name,
            "metadata": dict(message.metadata or {}),
        }

    @staticmethod
    def _is_summarizable(message: Message) -> bool:
        if not str(message.content or "").strip():
            return False
        if message.type == MessageType.MESSAGE:
            return message.role in (RoleType.USER, RoleType.ASSISTANT)
        if message.type != MessageType.CONTEXT_EVENT:
            return False
        metadata = message.metadata or {}
        event_type = str(metadata.get("event_type", "observation")).lower()
        return event_type not in {"heartbeat", "ping", "sensor_tick", "presence_tick"}
