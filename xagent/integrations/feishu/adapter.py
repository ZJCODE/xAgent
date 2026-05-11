"""FeishuChannel <-> xAgent bridge.

Routing (hardcoded, no config knobs — behaves like a real human):

* ``p2p`` (direct chat with the bot): ``agent.chat``.
* ``group`` / ``topic`` with bot @mentioned: ``agent.chat``.
* ``group`` / ``topic`` without @mention: ``agent.observe``
  (the agent itself decides whether to speak).
* Any other chat type is ignored.

User identity is the Feishu ``sender_id`` (open_id). Because xAgent's memory
layer is keyed by stable ``user_id``, that is the only choice that survives
across sessions without an extra API call.

Group replies are sent as plain replies anchored to the source message
(``reply_to``); never as Feishu topic/thread replies. p2p replies are sent
as fresh messages (no quoting).

The adapter is intentionally thin. Mention parsing, dedup, retries,
reconnection, and streaming cards are delegated to ``FeishuChannel``.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, Union

from pydantic import BaseModel

from ...core.agent import Agent
from .config import FeishuAdapterConfig
from .dedup import PersistentDedup, default_state_dir
from .history import FeishuHistoryFetcher, FeishuMessageRecord, format_context_recap
from .pending_history import PendingHistoryEntry, PendingHistoryStore
from .send import send_with_fallback


class _FeishuLogRedactionFilter(logging.Filter):
    """Redact short-lived Feishu WS credentials from SDK log lines."""

    _SECRET_QUERY_RE = re.compile(r"([?&](?:access_key|ticket)=)[^&\s\]]+")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = self._SECRET_QUERY_RE.sub(r"\1[redacted]", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


_LOG_REDACTION_FILTER = _FeishuLogRedactionFilter()


class FeishuAdapter:
    """Bridge between ``FeishuChannel`` events and an ``Agent`` instance."""

    def __init__(
        self,
        agent: Agent,
        config: FeishuAdapterConfig,
        *,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self._channel = None  # type: ignore[var-annotated]
        self._history_fetcher: Optional[FeishuHistoryFetcher] = None
        self._warned_mention_fallback = False
        self._stop_event = asyncio.Event()

        state_dir = (
            Path(config.dedup_state_dir).expanduser()
            if config.dedup_state_dir
            else default_state_dir()
        )
        self._dedup = PersistentDedup(
            namespace=config.app_id,
            state_dir=state_dir,
            logger=self.logger,
        )
        self._pending_history = PendingHistoryStore(
            max_per_chat=config.pending_history_size,
            ttl_seconds=config.pending_history_ttl_seconds,
        )
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._processing_tasks_lock = threading.Lock()
        self._processing_tasks: set[asyncio.Task[None]] = set()
        self._eager_bot_open_id: Optional[str] = None
        self._eager_bot_name: Optional[str] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _build_channel(self):
        try:
            from lark_oapi import LogLevel  # type: ignore
            from lark_oapi.channel import FeishuChannel  # type: ignore
            from lark_oapi.channel.config import PolicyConfig, SafetyConfig, TextBatchConfig  # type: ignore
        except ImportError as exc:  # pragma: no cover - import guard
            raise RuntimeError(
                "The Feishu adapter requires the 'lark-oapi' package. "
                "Install it with: pip install 'myxagent[feishu]'"
            ) from exc

        kwargs: dict[str, Any] = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret,
            "log_level": self._normalize_log_level(self.config.log_level, LogLevel),
        }
        if self.config.domain:
            kwargs["domain"] = self.config.domain
        if "policy" not in self.config.advanced:
            # Always receive every group/topic message; the adapter — not the
            # SDK policy gate — decides chat vs observe based on @mention.
            kwargs["policy"] = PolicyConfig(require_mention=False)
        if "safety" not in self.config.advanced:
            kwargs["safety"] = SafetyConfig(text_batch=TextBatchConfig(delay_ms=0))
        # Forward any advanced FeishuChannel kwargs (policy, safety, ...).
        for key, value in self.config.advanced.items():
            kwargs.setdefault(key, value)
        return FeishuChannel(**kwargs)

    @staticmethod
    def _normalize_log_level(value: Any, log_level_cls: Any) -> Any:
        """Convert user-friendly config strings into the SDK LogLevel enum."""
        if hasattr(value, "value"):
            return value
        normalized = str(value or "info").strip().upper()
        aliases = {"WARN": "WARNING"}
        normalized = aliases.get(normalized, normalized)
        try:
            return getattr(log_level_cls, normalized)
        except AttributeError as exc:
            allowed = ", ".join(name.lower() for name in log_level_cls.__members__)
            raise RuntimeError(
                f"Invalid Feishu log_level {value!r}. Expected one of: {allowed}."
            ) from exc

    async def run(self) -> None:
        """Connect to Feishu from an async application.

        The official SDK's WebSocket client owns a module-level event loop, so
        the blocking transport must be initialized outside the caller's running
        event loop.
        """
        loop = asyncio.get_running_loop()
        run_task = loop.run_in_executor(None, self.run_blocking)
        stop_task = asyncio.create_task(self._stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {run_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_task in done:
                self._safe_stop()
            for task in pending:
                task.cancel()
            if run_task in done:
                exc = run_task.exception()
                if exc is not None:
                    raise exc
        finally:
            self._safe_stop()

    def run_blocking(self) -> None:
        """Connect to Feishu and serve events until stopped.

        This is the preferred CLI/server entrypoint for the current
        ``lark-oapi`` WebSocket transport because its lower-level WS client
        manages a synchronous event loop internally.
        """
        self._install_log_redaction_filter()
        self._setup_channel()
        assert self._channel is not None

        self.logger.info("Connecting Feishu WebSocket (app_id=%s)…", self.config.app_id)
        try:
            self._channel.start()
        finally:
            self._safe_stop()

    @staticmethod
    def _install_log_redaction_filter() -> None:
        lark_logger = logging.getLogger("Lark")
        if not any(isinstance(item, _FeishuLogRedactionFilter) for item in lark_logger.filters):
            lark_logger.addFilter(_LOG_REDACTION_FILTER)

    def _setup_channel(self) -> None:
        self._channel = self._build_channel()
        self._channel.on("message", self._on_message)
        self._channel.on("error", self._on_error)
        self._channel.on("reject", self._on_reject)
        self._channel.on("reconnecting", self._on_reconnecting)
        self._channel.on("reconnected", self._on_reconnected)

    async def stop(self) -> None:
        """Request a graceful shutdown of the connect loop."""
        self._stop_event.set()
        self._safe_stop()

    def _safe_stop(self) -> None:
        self._cancel_processing_tasks()
        channel = self._channel
        if channel is None:
            return
        try:
            channel.stop()
        except Exception:  # pragma: no cover - best-effort cleanup
            self.logger.debug("FeishuChannel stop raised", exc_info=True)

    def _cancel_processing_tasks(self) -> None:
        with self._processing_tasks_lock:
            tasks = list(self._processing_tasks)
        for task in tasks:
            if task.done():
                continue
            try:
                loop = task.get_loop()
                if loop.is_running():
                    loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            except RuntimeError:  # pragma: no cover - loop already closed
                pass

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_message(self, msg: Any) -> None:
        task = asyncio.create_task(self._dispatch(msg))
        with self._processing_tasks_lock:
            self._processing_tasks.add(task)
        task.add_done_callback(self._on_dispatch_task_done)

    def _on_dispatch_task_done(self, task: asyncio.Task[None]) -> None:
        with self._processing_tasks_lock:
            self._processing_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.exception("Unhandled error while processing Feishu message")

    async def _on_error(self, err: Any) -> None:
        self.logger.error("FeishuChannel error: %s", err)

    def _on_reject(self, event: Any) -> None:
        self.logger.info(
            "Feishu message rejected: reason=%s chat_id=%s message_id=%s sender_id=%s",
            getattr(event, "reason", None),
            getattr(event, "chat_id", None),
            getattr(event, "message_id", None),
            getattr(event, "sender_id", None),
        )

    async def _on_reconnecting(self, *_: Any) -> None:
        self.logger.warning("FeishuChannel reconnecting…")

    async def _on_reconnected(self, *_: Any) -> None:
        self.logger.info("FeishuChannel reconnected.")

    # ------------------------------------------------------------------
    # Core routing
    # ------------------------------------------------------------------

    async def _dispatch(self, msg: Any) -> None:
        chat_type = getattr(msg, "chat_type", "unknown")
        chat_id = getattr(msg, "chat_id", None)
        message_id = getattr(msg, "message_id", None)
        sender_id = getattr(msg, "sender_id", None) or "feishu_user"
        sender_name = self._sender_name(msg) or sender_id
        text = self._message_text(msg)

        self.logger.debug(
            "Feishu inbound: chat_type=%s chat_id=%s message_id=%s sender_id=%s text=%r",
            chat_type,
            chat_id,
            message_id,
            sender_id,
            (text[:120] + "…") if len(text) > 120 else text,
        )

        if not chat_id:
            self.logger.debug("Skipping message without chat_id: %r", msg)
            return

        # Always record observable group/topic content so the next @-mention
        # in this chat can be primed with recent context — independent of
        # the routing decision below.
        if chat_type in {"group", "topic"} and text:
            self._pending_history.record(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
            )

        # Persistent dedup: protects against SDK redelivery on WS reconnect
        # and process restarts. We claim BEFORE the per-chat lock so the
        # duplicate check is cheap and never blocks behind a slow turn.
        claim = self._dedup.try_begin(message_id)
        if claim == "duplicate":
            self.logger.info(
                "Feishu message already processed; skipping duplicate: chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            return
        if claim == "inflight":
            self.logger.info(
                "Feishu message already inflight; skipping: chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            return
        if claim == "invalid":
            self.logger.debug("Feishu message missing message_id; processing without dedup")

        finalize_id = message_id if claim == "claimed" else None

        # Per-chat serialization: prevents a slow turn from starving later
        # @-mentions in the SAME chat. Different chats remain parallel.
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock

        try:
            async with lock:
                await self._route(
                    msg=msg,
                    chat_type=chat_type,
                    chat_id=chat_id,
                    message_id=message_id,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                )
        finally:
            if finalize_id is not None:
                self._dedup.finalize(finalize_id)

    async def _route(
        self,
        *,
        msg: Any,
        chat_type: str,
        chat_id: str,
        message_id: Optional[str],
        sender_id: str,
        sender_name: str,
        text: str,
    ) -> None:
        if chat_type == "p2p":
            if not text:
                self.logger.debug("Skipping non-text direct message")
                return
            await self._handle_chat(
                chat_id=chat_id,
                message_id=message_id,
                user_id=sender_id,
                text=text,
                is_group=False,
                raw_msg=msg,
            )
            return

        if chat_type in {"group", "topic"}:
            if self._is_bot_mentioned(msg):
                if not text:
                    text = "The user mentioned you without adding any text."
                self.logger.info(
                    "Feishu @mention routed to chat: chat_type=%s chat_id=%s message_id=%s sender_id=%s",
                    chat_type,
                    chat_id,
                    message_id,
                    sender_id,
                )
                await self._handle_chat(
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=sender_id,
                    text=text,
                    is_group=True,
                    raw_msg=msg,
                )
                return
            if not text:
                self.logger.debug("Skipping non-text group message (chat_type=%s)", chat_type)
                return
            await self._handle_observe(
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
            )
            return

        self.logger.debug("Ignoring chat_type=%s", chat_type)

    @staticmethod
    def _message_text(msg: Any) -> str:
        content_text = (getattr(msg, "content_text", None) or "").strip()
        if content_text:
            return content_text
        content = getattr(msg, "content", None)
        for field_name in ("text", "title"):
            value = getattr(content, field_name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _sender_name(msg: Any) -> Optional[str]:
        for field_name in ("sender_name", "user_name", "name"):
            value = getattr(msg, field_name, None)
            if isinstance(value, str) and value.strip():
                return value.strip()

        for nested_name in ("sender", "user"):
            nested = getattr(msg, nested_name, None)
            if nested is None:
                continue
            for field_name in ("name", "sender_name", "user_name", "display_name"):
                value = getattr(nested, field_name, None)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _format_observe_context(*, sender_name: str, text: str) -> str:
        return (
            "[ambient_context]\n"
            f"In the Feishu group, user {sender_name} said:\n"
            f"{text}"
        )

    def _is_bot_mentioned(self, msg: Any) -> bool:
        if bool(getattr(msg, "mentioned_bot", False)):
            return True

        mentions = list(getattr(msg, "mentions", []) or [])
        if not mentions:
            return False

        bot_open_id = self._bot_open_id()
        if bot_open_id:
            return any(self._mention_field(mention, "open_id") == bot_open_id for mention in mentions)

        bot_name = self._bot_name()
        if bot_name and any(self._mention_field(mention, "name") == bot_name for mention in mentions):
            return True

        # The SDK can receive group @ events before bot identity has resolved.
        # In that window `mentioned_bot` is false and we cannot compare open_id.
        # Treat mentioned group/topic messages as addressed so direct @bot does
        # not go silent; once identity resolves the precise open_id path above
        # takes over.
        if not self._warned_mention_fallback:
            self.logger.warning(
                "Bot identity is not resolved; treating mentioned group/topic message as @bot"
            )
            self._warned_mention_fallback = True
        return True

    @staticmethod
    def _mention_field(mention: Any, field_name: str) -> Optional[str]:
        if isinstance(mention, dict):
            value = mention.get(field_name)
            if isinstance(value, str) and value:
                return value
            mention_id = mention.get("id")
            if isinstance(mention_id, dict):
                nested = mention_id.get(field_name)
                if isinstance(nested, str) and nested:
                    return nested
            return None
        value = getattr(mention, field_name, None)
        if isinstance(value, str) and value:
            return value
        mention_id = getattr(mention, "id", None)
        nested = getattr(mention_id, field_name, None) if mention_id is not None else None
        if isinstance(nested, str) and nested:
            return nested
        return None

    def _bot_open_id(self) -> Optional[str]:
        channel = self._channel
        if channel is None:
            return None
        identity = getattr(channel, "bot_identity", None)
        open_id = getattr(identity, "open_id", None)
        if open_id:
            return open_id
        return getattr(channel, "_bot_open_id", None)

    def _bot_name(self) -> Optional[str]:
        channel = self._channel
        if channel is None:
            return None
        identity = getattr(channel, "bot_identity", None)
        name = getattr(identity, "name", None)
        if isinstance(name, str) and name:
            return name
        return None

    # ------------------------------------------------------------------
    # Context prefetch (observe-then-chat)
    # ------------------------------------------------------------------

    def _get_history_fetcher(self) -> Optional[FeishuHistoryFetcher]:
        if not self.config.prefetch_context:
            return None
        if self._channel is None:
            return None
        if self._history_fetcher is None:
            self._history_fetcher = FeishuHistoryFetcher(self._channel, self.logger)
        return self._history_fetcher

    def _records_from_pending(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        limit: int,
    ) -> list[FeishuMessageRecord]:
        if limit <= 0:
            return []
        entries = self._pending_history.peek(
            chat_id=chat_id,
            exclude_message_id=current_message_id,
            limit=limit,
        )
        records: list[FeishuMessageRecord] = []
        for entry in entries:
            records.append(
                FeishuMessageRecord(
                    message_id=entry.message_id or "",
                    sender_id=entry.sender_id,
                    sender_name=entry.sender_name,
                    text=entry.text,
                    create_time_ms=entry.timestamp_ms,
                    source="history",
                )
            )
        return records

    @staticmethod
    def _merge_context_records(
        local_records: list[FeishuMessageRecord],
        api_records: list[FeishuMessageRecord],
        *,
        current_message_id: Optional[str],
    ) -> list[FeishuMessageRecord]:
        merged: dict[str, FeishuMessageRecord] = {}

        def key_for(record: FeishuMessageRecord) -> str:
            if record.message_id:
                return record.message_id
            return f"{record.sender_id}:{record.create_time_ms}:{record.text}"

        for record in [*api_records, *local_records]:
            if current_message_id and record.message_id == current_message_id:
                continue
            key = key_for(record)
            existing = merged.get(key)
            if existing is None or (not existing.sender_name and record.sender_name):
                merged[key] = record
        return sorted(merged.values(), key=lambda item: item.create_time_ms)

    async def _prime_context(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        raw_msg: Any,
        is_group: bool,
        user_id: str,
    ) -> None:
        parent_id = self._reply_to_message_id(raw_msg)
        thread_id = self._thread_id(raw_msg) if is_group else None
        history_count = self.config.chat_history_count if is_group else 0

        records: list[FeishuMessageRecord] = []

        # Primary source: messages observed live in this chat. This avoids
        # the slow / permission-fragile history API for the common case
        # (rapid @-mentions in an active group).
        if is_group and history_count > 0:
            records = self._records_from_pending(
                chat_id=chat_id,
                current_message_id=current_message_id,
                limit=history_count,
            )

        # Supplement from the official history API whenever prefetch is
        # enabled. Pending history is fast and catches events immediately;
        # the API fills gaps from WS reconnect windows or SDK delivery delays.
        needs_api = parent_id or thread_id or history_count > 0
        if needs_api:
            fetcher = self._get_history_fetcher()
            if fetcher is not None:
                try:
                    # Hard cap the prefetch so a slow / unauthorized Feishu
                    # API never blocks the @-mention reply.
                    api_records = await asyncio.wait_for(
                        fetcher.fetch_context(
                            chat_id=chat_id,
                            current_message_id=current_message_id,
                            parent_message_id=parent_id,
                            thread_id=thread_id,
                            history_count=history_count,
                        ),
                        timeout=self.config.prefetch_timeout,
                    )
                except asyncio.TimeoutError:
                    self.logger.warning(
                        "Feishu context prefetch timed out after %.1fs; continuing without it",
                        self.config.prefetch_timeout,
                    )
                    api_records = []
                except Exception:
                    self.logger.exception(
                        "Feishu context prefetch failed; continuing without it"
                    )
                    api_records = []
                if api_records:
                    records = self._merge_context_records(
                        local_records=records,
                        api_records=api_records,
                        current_message_id=current_message_id,
                    )

        if not records:
            self.logger.debug(
                "No Feishu context to prime (chat_id=%s, parent=%s, thread=%s, n=%d)",
                chat_id,
                parent_id,
                thread_id,
                history_count,
            )
            return

        recap = format_context_recap(records, bot_open_id=self._bot_open_id())
        if not recap.strip():
            return

        self.logger.info(
            "Priming agent with %d Feishu context message(s) before chat (chat_id=%s)",
            len(records),
            chat_id,
        )

        observe_kwargs: dict[str, Any] = {
            "context": recap,
            "current_user_id": user_id,
            "source": "feishu",
            "event_type": "history_recap",
            "sender_id": user_id,
            "metadata": {
                "chat_id": chat_id,
                "current_message_id": current_message_id,
                "addressed_to_agent": False,
                "context_only": True,
                "record_count": len(records),
                "record_sources": sorted({r.source for r in records}),
            },
            "enable_memory": self.config.enable_memory,
            # Ingest-only: no LLM call, no chance of swallowing the @-reply.
            "no_reply": True,
        }
        if self.config.max_concurrent_tools is not None:
            observe_kwargs["max_concurrent_tools"] = self.config.max_concurrent_tools

        try:
            await self.agent.observe(**observe_kwargs)
        except Exception:
            self.logger.exception("agent.observe(history_recap) failed; continuing")

    @staticmethod
    def _reply_to_message_id(msg: Any) -> Optional[str]:
        if msg is None:
            return None
        # Typed InboundMessage exposes `reply_to_message_id`.
        direct = getattr(msg, "reply_to_message_id", None)
        if isinstance(direct, str) and direct:
            return direct
        reply = getattr(msg, "reply", None)
        if reply is None:
            return None
        nested = getattr(reply, "message_id", None)
        if isinstance(nested, str) and nested:
            return nested
        if isinstance(reply, dict):
            value = reply.get("message_id")
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _thread_id(msg: Any) -> Optional[str]:
        if msg is None:
            return None
        direct = getattr(msg, "thread_id", None)
        if isinstance(direct, str) and direct:
            return direct
        conversation = getattr(msg, "conversation", None)
        if conversation is None:
            return None
        thread_id = getattr(conversation, "thread_id", None)
        if isinstance(thread_id, str) and thread_id:
            return thread_id
        if isinstance(conversation, dict):
            value = conversation.get("thread_id")
            if isinstance(value, str) and value:
                return value
        return None

    # ------------------------------------------------------------------
    # Chat path
    # ------------------------------------------------------------------

    async def _handle_chat(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        user_id: str,
        text: str,
        is_group: bool,
        raw_msg: Any = None,
    ) -> None:
        # Scroll up: pull surrounding context the bot didn't see, and prime
        # it through ``agent.observe`` (like a human catching up before
        # speaking). Safe no-op when no context is available or the app
        # lacks history-read scopes.
        await self._prime_context(
            chat_id=chat_id,
            current_message_id=message_id,
            raw_msg=raw_msg,
            is_group=is_group,
            user_id=user_id,
        )

        chat_kwargs = self._chat_kwargs(user_id=user_id, text=text)

        if self.config.stream and not self.agent.output_type:
            await self._send_streaming(
                chat_id=chat_id,
                message_id=message_id,
                is_group=is_group,
                chat_kwargs={**chat_kwargs, "stream": True},
                raw_msg=raw_msg,
            )
            return

        result = await self.agent.chat(**chat_kwargs)
        reply_text = self._stringify(result)
        if not reply_text:
            self.logger.warning(
                "Agent returned empty Feishu reply: chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            return
        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        await self._send_markdown(
            chat_id=chat_id,
            message_id=anchor,
            text=reply_text,
            is_group=is_group,
            is_p2p=not is_group,
        )
    def _chat_kwargs(self, *, user_id: str, text: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user_message": text,
            "user_id": user_id,
            "enable_memory": self.config.enable_memory,
        }
        if self.config.history_count is not None:
            kwargs["history_count"] = self.config.history_count
        if self.config.max_iter is not None:
            kwargs["max_iter"] = self.config.max_iter
        if self.config.max_concurrent_tools is not None:
            kwargs["max_concurrent_tools"] = self.config.max_concurrent_tools
        return kwargs

    async def _send_streaming(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        is_group: bool,
        chat_kwargs: dict[str, Any],
        raw_msg: Any = None,
    ) -> None:
        agent_stream = await self.agent.chat(**chat_kwargs)
        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        if not hasattr(agent_stream, "__aiter__"):
            # Agent fell back to non-stream (e.g. structured output); use the
            # plain reply path.
            reply_text = self._stringify(agent_stream)
            if reply_text:
                await self._send_markdown(
                    chat_id=chat_id,
                    message_id=anchor,
                    text=reply_text,
                    is_group=is_group,
                    is_p2p=not is_group,
                )
            return

        async def producer(stream):
            async for chunk in agent_stream:  # type: ignore[func-returns-value]
                if chunk:
                    await stream.append(chunk)

        opts = self._send_opts(message_id=anchor, is_group=is_group)
        assert self._channel is not None
        result = await self._channel.stream(chat_id, {"markdown": producer}, opts)
        self._log_send_result(result=result, chat_id=chat_id, message_id=anchor)

    # ------------------------------------------------------------------
    # Observe path
    # ------------------------------------------------------------------

    async def _handle_observe(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        sender_id: str,
        sender_name: str,
        text: str,
    ) -> None:
        observe_kwargs: dict[str, Any] = {
            "context": self._format_observe_context(sender_name=sender_name, text=text),
            "current_user_id": sender_id,
            "source": "feishu",
            "event_type": "group_message",
            "sender_id": sender_id,
            "metadata": {
                "chat_id": chat_id,
                "message_id": message_id,
                "sender_name": sender_name,
                "addressed_to_agent": False,
            },
            "enable_memory": self.config.enable_memory,
        }
        if self.config.history_count is not None:
            observe_kwargs["history_count"] = self.config.history_count
        if self.config.max_iter is not None:
            observe_kwargs["max_iter"] = self.config.max_iter
        if self.config.max_concurrent_tools is not None:
            observe_kwargs["max_concurrent_tools"] = self.config.max_concurrent_tools

        result = await self.agent.observe(**observe_kwargs)
        if not getattr(result, "replied", False):
            return
        reply_text = result.reply or ""
        if not reply_text.strip():
            return
        # Observed replies are unsolicited — do NOT thread-reply to the
        # message that triggered them; send as a new message into the chat.
        await self._send_markdown(
            chat_id=chat_id,
            message_id=None,
            text=reply_text,
            is_group=True,
            is_p2p=False,
        )

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    def _send_opts(
        self,
        *,
        message_id: Optional[str],
        is_group: bool,
    ) -> Optional[dict[str, Any]]:
        # p2p: send as a fresh message (no quoting).
        # group/topic: anchor reply to the source message, but never as a
        # Feishu topic/thread reply.
        if not message_id or not is_group:
            return None
        return {"reply_to": message_id}

    def _reply_anchor(
        self,
        *,
        raw_msg: Any,
        message_id: Optional[str],
    ) -> Optional[str]:
        """Pick the right reply anchor for the current message.

        For topic groups (话题群), anchoring to the triggering message
        pushes the reply into a hidden sub-thread the user does not see.
        We anchor to the topic's ``root`` message instead so the reply
        renders in the main chat view, matching how human users reply.
        For normal groups and p2p, the triggering message id is used.
        """
        if raw_msg is None:
            return message_id
        chat_type = getattr(raw_msg, "chat_type", None)
        if chat_type == "topic":
            root_id = self._root_message_id(raw_msg)
            if root_id:
                return root_id
        return message_id

    @staticmethod
    def _root_message_id(msg: Any) -> Optional[str]:
        if msg is None:
            return None
        for attr in ("root_id", "root_message_id"):
            value = getattr(msg, attr, None)
            if isinstance(value, str) and value:
                return value
        return None

    async def _send_markdown(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        text: str,
        is_group: bool,
        is_p2p: Optional[bool] = None,
    ) -> None:
        assert self._channel is not None
        if is_p2p is None:
            is_p2p = not is_group
        try:
            await send_with_fallback(
                self._channel,
                chat_id=chat_id,
                payload={"markdown": text},
                reply_to=message_id if (is_group and message_id) else None,
                is_p2p=is_p2p,
                logger=self.logger,
                message_id=message_id,
            )
        except Exception:
            self.logger.exception("Failed to send Feishu message to %s", chat_id)

    def _log_send_result(self, *, result: Any, chat_id: str, message_id: Optional[str]) -> None:
        if getattr(result, "success", True):
            return
        error = getattr(result, "error", None)
        self.logger.error(
            "Failed to send Feishu message: chat_id=%s reply_to=%s error=%s raw=%s",
            chat_id,
            message_id,
            error,
            getattr(result, "raw", None),
        )

    @staticmethod
    def _stringify(
        reply: Union[str, BaseModel, AsyncGenerator[str, None], Any],
    ) -> str:
        if reply is None:
            return ""
        if isinstance(reply, str):
            return reply.strip()
        if isinstance(reply, BaseModel):
            return reply.model_dump_json(indent=2)
        return str(reply).strip()
