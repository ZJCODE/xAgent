"""FeishuChannel <-> xAgent bridge.

Routing is intentionally small:

* ``p2p`` (direct chat with the bot): reply with ``agent.chat``.
* ``group`` / ``topic`` with bot @mentioned: pull recent Feishu history,
    then reply with ``agent.chat``.
* ``group`` / ``topic`` without @mention: ignore.
* Any other chat type is ignored.

Before a Feishu message reaches the agent, the sender ID is resolved to a
display name through the official contact API. By default, internal
``ou_`` / ``on_`` IDs stay inside this adapter and are not passed into
``agent.chat``. When ``show_sender_ids`` is enabled, group room context can
render speakers as ``name(id)``.

Group replies are sent as plain replies anchored to the source message
(``reply_to``); never as Feishu topic/thread replies. p2p replies are sent
as fresh messages (no quoting).

The adapter is intentionally thin. Mention parsing, reconnection, and
streaming cards are delegated to ``FeishuChannel``.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import re
import threading
import time
from typing import Any, AsyncGenerator, Optional, Union

from pydantic import BaseModel

from ...core.agent import Agent
from .config import FeishuAdapterConfig
from .history import (
    FeishuHistoryFetcher,
    FeishuMessageRecord,
    format_room_context,
    replace_mentions,
    sanitize_transcript_field,
)
from .send import send_message
from .users import FEISHU_USER_FALLBACK_NAME, FeishuUserResolver, extract_feishu_id, safe_display_name


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
        show_sender_ids: Optional[bool] = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.show_sender_ids = config.show_sender_ids if show_sender_ids is None else show_sender_ids
        self._channel = None  # type: ignore[var-annotated]
        self._history_fetcher: Optional[FeishuHistoryFetcher] = None
        self._user_resolver: Optional[FeishuUserResolver] = None
        self._room_name_cache: dict[str, str] = {}
        self._warned_mention_fallback = False
        self._stop_event = asyncio.Event()
        self._owner_loop: Optional[asyncio.AbstractEventLoop] = None
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._processing_tasks_lock = threading.Lock()
        self._processing_tasks: set[asyncio.Task[None]] = set()

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
                "Install it with: pip install myxagent"
            ) from exc

        kwargs: dict[str, Any] = {
            "app_id": self.config.app_id,
            "app_secret": self.config.app_secret,
            "log_level": self._normalize_log_level(self.config.log_level, LogLevel),
        }
        if self.config.domain:
            kwargs["domain"] = self.config.domain
        if "policy" not in self.config.advanced:
            # Let the SDK filter group traffic to @mentions. The adapter still
            # checks mentions itself because identity can resolve late.
            kwargs["policy"] = PolicyConfig(require_mention=True)
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
        self._owner_loop = loop
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
            await self._flush_agent_memory()
            self._owner_loop = None

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
        await self._flush_agent_memory()

    async def _flush_agent_memory(self) -> None:
        flusher = getattr(self.agent, "flush_memory", None)
        if flusher is not None:
            await flusher()

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
        owner_loop = self._owner_loop
        if owner_loop is not None and owner_loop.is_running():
            try:
                current_loop = asyncio.get_running_loop()
            except RuntimeError:
                current_loop = None
            if current_loop is not owner_loop:
                owner_loop.call_soon_threadsafe(self._create_dispatch_task, msg)
                return
        self._create_dispatch_task(msg)

    def _create_dispatch_task(self, msg: Any) -> None:
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
        chat_type = self._message_field(msg, "chat_type") or "unknown"
        chat_id = self._message_field(msg, "chat_id")
        message_id = self._message_field(msg, "message_id")
        sender_id = self._sender_id(msg) or ""
        sender_fallback_name = self._sender_name(msg)
        sender_type = self._sender_type(msg)
        sender_id_type = self._sender_id_type(msg)
        text = self._message_text(msg, show_mention_ids=self.show_sender_ids)

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

        if self._should_ignore_sender(sender_id, sender_type, chat_type):
            self.logger.debug(
                "Ignoring Feishu message from current bot/app sender: chat_id=%s message_id=%s",
                chat_id,
                message_id,
            )
            return

        # Per-chat serialization: prevents a slow turn from starving later
        # @-mentions in the SAME chat. Different chats remain parallel.
        lock = self._chat_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[chat_id] = lock

        async with lock:
            await self._route(
                msg=msg,
                chat_type=chat_type,
                chat_id=chat_id,
                message_id=message_id,
                sender_id=sender_id,
                sender_id_type=sender_id_type,
                sender_type=sender_type,
                sender_fallback_name=sender_fallback_name,
                text=text,
            )

    async def _route(
        self,
        *,
        msg: Any,
        chat_type: str,
        chat_id: str,
        message_id: Optional[str],
        sender_id: str,
        sender_id_type: Optional[str],
        sender_type: str,
        sender_fallback_name: Optional[str],
        text: str,
    ) -> None:
        if chat_type == "p2p":
            if not text:
                self.logger.debug("Skipping non-text direct message")
                return
            sender_name = await self._resolve_sender_name(
                sender_id,
                fallback=sender_fallback_name,
                id_type=sender_id_type,
                sender_type=sender_type,
            )
            await self._handle_chat(
                chat_id=chat_id,
                message_id=message_id,
                user_id=sender_name,
                sender_id=sender_id,
                sender_name=sender_name,
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
                sender_name = await self._resolve_sender_name(
                    sender_id,
                    fallback=sender_fallback_name,
                    id_type=sender_id_type,
                    sender_type=sender_type,
                )
                await self._handle_chat(
                    chat_id=chat_id,
                    message_id=message_id,
                    user_id=sender_name,
                    sender_id=sender_id,
                    sender_name=sender_name,
                    text=text,
                    is_group=True,
                    raw_msg=msg,
                )
                return
            self.logger.debug(
                "Ignoring unmentioned Feishu group message: chat_type=%s chat_id=%s message_id=%s",
                chat_type,
                chat_id,
                message_id,
            )
            return

        self.logger.debug("Ignoring chat_type=%s", chat_type)

    @staticmethod
    def _message_text(msg: Any, *, show_mention_ids: bool = False) -> str:
        mentions = FeishuAdapter._message_mentions(msg)
        content_text = FeishuAdapter._object_field(FeishuAdapter._message_object(msg) or msg, "content_text") or ""
        content_text = content_text.strip()
        if content_text:
            return replace_mentions(content_text, mentions, show_mention_ids=show_mention_ids)
        content = FeishuAdapter._message_field(msg, "content")
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (TypeError, ValueError):
                content = content.strip()
        for field_name in ("text", "title"):
            value = FeishuAdapter._raw_field(content, field_name)
            if isinstance(value, str) and value.strip():
                return replace_mentions(value.strip(), mentions, show_mention_ids=show_mention_ids)
        return ""

    @staticmethod
    def _sender_id(msg: Any) -> Optional[str]:
        sender_id, _sender_id_type = FeishuAdapter._sender_identity(msg)
        return sender_id

    @staticmethod
    def _sender_type(msg: Any) -> str:
        value = FeishuAdapter._object_field(msg, "sender_type")
        if value:
            return value.lower()
        sender = FeishuAdapter._sender_object(msg)
        value = FeishuAdapter._object_field(sender, "sender_type")
        return value.lower() if value else ""

    @staticmethod
    def _sender_id_type(msg: Any) -> Optional[str]:
        _sender_id, sender_id_type = FeishuAdapter._sender_identity(msg)
        return sender_id_type

    @staticmethod
    def _sender_identity(msg: Any) -> tuple[Optional[str], Optional[str]]:
        sender = FeishuAdapter._sender_object(msg)
        explicit_id_type = FeishuAdapter._explicit_sender_id_type(msg, sender)
        candidates = (
            FeishuAdapter._raw_field(msg, "sender_id"),
            FeishuAdapter._raw_field(sender, "sender_id"),
            FeishuAdapter._raw_field(sender, "id"),
            sender,
        )
        for candidate in candidates:
            sender_id, sender_id_type = extract_feishu_id(candidate)
            if sender_id:
                return sender_id, explicit_id_type or sender_id_type
        return None, explicit_id_type

    @staticmethod
    def _explicit_sender_id_type(msg: Any, sender: Any) -> Optional[str]:
        for container in (msg, sender):
            for field_name in ("sender_id_type", "id_type", "user_id_type"):
                value = FeishuAdapter._object_field(container, field_name)
                if value:
                    return value.lower()
        return None

    @staticmethod
    def _sender_object(msg: Any) -> Any:
        sender = FeishuAdapter._raw_field(msg, "sender")
        if sender is not None:
            return sender
        event = FeishuAdapter._raw_field(msg, "event")
        return FeishuAdapter._raw_field(event, "sender")

    @staticmethod
    def _message_object(msg: Any) -> Any:
        event = FeishuAdapter._raw_field(msg, "event")
        message = FeishuAdapter._raw_field(event, "message")
        if message is not None:
            return message
        return FeishuAdapter._raw_field(msg, "message")

    @staticmethod
    def _message_field(msg: Any, field_name: str) -> Any:
        value = FeishuAdapter._raw_field(msg, field_name)
        if value is not None:
            return value
        message = FeishuAdapter._message_object(msg)
        return FeishuAdapter._raw_field(message, field_name)

    @staticmethod
    def _message_mentions(msg: Any) -> list[Any]:
        mentions = FeishuAdapter._message_field(msg, "mentions") or []
        return list(mentions) if isinstance(mentions, (list, tuple)) else []

    def _should_ignore_sender(self, sender_id: str, sender_type: str, chat_type: str) -> bool:
        if self._is_current_bot_sender(sender_id):
            return True
        return chat_type == "p2p" and sender_type in {"bot", "app"}

    def _is_current_bot_sender(self, sender_id: str) -> bool:
        if not sender_id:
            return False
        return sender_id in {self.config.app_id, self._bot_open_id()}

    @staticmethod
    def _object_field(obj: Any, field_name: str) -> Optional[str]:
        value = FeishuAdapter._raw_field(obj, field_name)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _raw_field(obj: Any, field_name: str) -> Any:
        if obj is None:
            return None
        if isinstance(obj, dict):
            return obj.get(field_name)
        return getattr(obj, field_name, None)

    @staticmethod
    def _sender_name(msg: Any) -> Optional[str]:
        for field_name in ("sender_name", "user_name", "name"):
            value = FeishuAdapter._object_field(msg, field_name)
            if value:
                return value
        sender = FeishuAdapter._sender_object(msg)
        for nested_name in ("sender", "user"):
            nested = sender if nested_name == "sender" else FeishuAdapter._raw_field(msg, nested_name)
            if nested is None:
                continue
            for field_name in ("name", "sender_name", "user_name", "display_name", "app_name", "bot_name"):
                value = FeishuAdapter._object_field(nested, field_name)
                if value:
                    return value
        return None

    def _get_user_resolver(self) -> Optional[FeishuUserResolver]:
        if self._channel is None:
            return None
        if self._user_resolver is None:
            self._user_resolver = FeishuUserResolver(self._channel, self.logger)
        return self._user_resolver

    async def _resolve_sender_name(
        self,
        sender_id: str,
        *,
        fallback: Optional[str],
        id_type: Optional[str] = None,
        sender_type: Optional[str] = None,
    ) -> str:
        resolver = self._get_user_resolver()
        if resolver is None:
            return safe_display_name(fallback) or FEISHU_USER_FALLBACK_NAME
        name = await resolver.resolve_name(
            sender_id,
            fallback=fallback,
            id_type=id_type,
            sender_type=sender_type,
        )
        return name or FEISHU_USER_FALLBACK_NAME

    def _is_bot_mentioned(self, msg: Any) -> bool:
        if bool(self._message_field(msg, "mentioned_bot")):
            return True

        mentions = self._message_mentions(msg)
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
        value = FeishuAdapter._object_field(mention, field_name)
        if isinstance(value, str) and value:
            return value
        if field_name in {"open_id", "user_id", "union_id", "app_id"}:
            mention_id, mention_id_type = extract_feishu_id(mention)
            if mention_id_type == field_name:
                return mention_id
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
    # Group history context
    # ------------------------------------------------------------------

    def _get_history_fetcher(self) -> Optional[FeishuHistoryFetcher]:
        if self._channel is None:
            return None
        if self._history_fetcher is None:
            self._history_fetcher = FeishuHistoryFetcher(
                self._channel,
                self.logger,
                user_resolver=self._get_user_resolver(),
            )
        return self._history_fetcher

    async def _fetch_group_history(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        raw_msg: Any,
    ) -> list[FeishuMessageRecord]:
        history_count = self.config.group_history_count
        if history_count <= 0:
            return []

        fetcher = self._get_history_fetcher()
        if fetcher is None:
            return []

        try:
            return await asyncio.wait_for(
                fetcher.fetch_recent_messages(
                    chat_id=chat_id,
                    current_message_id=current_message_id,
                    thread_id=self._thread_id(raw_msg),
                    history_count=history_count,
                    show_sender_ids=self.show_sender_ids,
                ),
                timeout=self.config.history_fetch_timeout,
            )
        except asyncio.TimeoutError:
            self.logger.warning(
                "Feishu group history fetch timed out after %.1fs; continuing without it",
                self.config.history_fetch_timeout,
            )
        except Exception:
            self.logger.exception("Feishu group history fetch failed; continuing without it")
        return []

    async def _chat_text_with_group_history(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        raw_msg: Any,
        sender_id: str,
        sender_name: str,
        text: str,
    ) -> str:
        records = await self._fetch_group_history(
            chat_id=chat_id,
            current_message_id=current_message_id,
            raw_msg=raw_msg,
        )
        room_name = await self._resolve_room_name(chat_id, raw_msg)
        current_record = FeishuMessageRecord(
            current_message_id or "",
            sender_id,
            sender_name,
            text,
            self._message_create_time_ms(raw_msg),
        )
        context_records = [*records, current_record]

        return format_room_context(
            chat_id,
            context_records,
            room_name=room_name,
            bot_open_id=self._bot_open_id(),
            bot_app_id=self.config.app_id,
            show_sender_ids=self.show_sender_ids,
        )

    async def _resolve_room_name(self, chat_id: str, raw_msg: Any) -> Optional[str]:
        event_room_name = self._message_room_name(raw_msg)
        if event_room_name:
            self._room_name_cache[chat_id] = event_room_name
            return event_room_name

        cached = self._room_name_cache.get(chat_id)
        if cached:
            return cached

        room_name = await self._fetch_room_name(chat_id)
        if room_name:
            self._room_name_cache[chat_id] = room_name
        return room_name

    @staticmethod
    def _message_room_name(msg: Any) -> Optional[str]:
        if msg is None:
            return None
        message = FeishuAdapter._message_object(msg) or msg
        for field_name in ("chat_name", "group_name", "room_name"):
            value = FeishuAdapter._raw_field(message, field_name)
            safe_value = sanitize_transcript_field(value)
            if safe_value:
                return safe_value
        for nested_name in ("chat", "conversation"):
            nested = FeishuAdapter._raw_field(message, nested_name)
            if nested is None:
                continue
            for field_name in ("chat_name", "group_name", "room_name", "name", "title"):
                value = FeishuAdapter._raw_field(nested, field_name)
                safe_value = sanitize_transcript_field(value)
                if safe_value:
                    return safe_value
        return None

    async def _fetch_room_name(self, chat_id: str) -> Optional[str]:
        channel = self._channel
        client = getattr(channel, "client", None)
        if client is None or not chat_id:
            return None
        try:
            from lark_oapi.api.im.v1 import GetChatRequest  # type: ignore
        except ImportError:  # pragma: no cover - import guard
            return None

        request = GetChatRequest.builder().chat_id(chat_id).user_id_type("open_id").build()
        try:
            getter = client.im.v1.chat.get
            response = await asyncio.to_thread(getter, request)
            if inspect.isawaitable(response):
                response = await response
        except Exception as exc:
            self.logger.info("Feishu get chat failed (chat_id=%s): %s", chat_id, exc)
            return None

        if not getattr(response, "success", lambda: False)():
            self.logger.info(
                "Feishu get chat rejected: code=%s msg=%s log_id=%s",
                getattr(response, "code", None),
                getattr(response, "msg", None),
                response.get_log_id() if hasattr(response, "get_log_id") else None,
            )
            return None

        data = getattr(response, "data", None)
        room_name = getattr(data, "name", None)
        if room_name is None:
            chat = getattr(data, "chat", None)
            room_name = getattr(chat, "name", None)
        return sanitize_transcript_field(room_name)

    @staticmethod
    def _message_create_time_ms(msg: Any) -> int:
        if msg is None:
            return int(time.time() * 1000)
        for field_name in ("create_time", "create_time_ms", "timestamp", "message_time", "event_time"):
            value = FeishuAdapter._message_field(msg, field_name)
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return int(time.time() * 1000)

    @staticmethod
    def _thread_id(msg: Any) -> Optional[str]:
        if msg is None:
            return None
        direct = FeishuAdapter._message_field(msg, "thread_id")
        if isinstance(direct, str) and direct:
            return direct
        message = FeishuAdapter._message_object(msg) or msg
        conversation = FeishuAdapter._raw_field(message, "conversation")
        if conversation is None:
            return None
        thread_id = FeishuAdapter._raw_field(conversation, "thread_id")
        if isinstance(thread_id, str) and thread_id:
            return thread_id
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
        sender_id: str,
        sender_name: str,
        text: str,
        is_group: bool,
        raw_msg: Any = None,
    ) -> None:
        chat_text = text
        if is_group:
            chat_text = await self._chat_text_with_group_history(
                chat_id=chat_id,
                current_message_id=message_id,
                raw_msg=raw_msg,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
            )

        chat_kwargs = self._chat_kwargs(
            user_id=user_id,
            text=chat_text,
        )

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
            uuid_message_id=message_id,
            text=reply_text,
            is_group=is_group,
        )

    def _chat_kwargs(
        self,
        *,
        user_id: str,
        text: str,
    ) -> dict[str, Any]:
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
        chat_events = getattr(self.agent, "chat_events", None)
        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        if callable(chat_events):
            sent_count = 0
            async for event in chat_events(**{k: v for k, v in chat_kwargs.items() if k != "stream"}):
                event_type = event.get("type")
                if event_type == "message_done":
                    content = str(event.get("content") or "").strip()
                    if not content:
                        continue
                    sent_count += 1
                    uuid_message_id = f"{message_id}:{sent_count}" if message_id else None
                    await self._send_markdown(
                        chat_id=chat_id,
                        message_id=anchor,
                        uuid_message_id=uuid_message_id,
                        text=content,
                        is_group=is_group,
                    )
                elif event_type == "error":
                    sent_count += 1
                    uuid_message_id = f"{message_id}:{sent_count}" if message_id else None
                    await self._send_markdown(
                        chat_id=chat_id,
                        message_id=anchor,
                        uuid_message_id=uuid_message_id,
                        text=str(event.get("error") or "Agent processing error."),
                        is_group=is_group,
                    )
            return

        agent_stream = await self.agent.chat(**chat_kwargs)
        if not hasattr(agent_stream, "__aiter__"):
            # Agent fell back to non-stream (e.g. structured output); use the
            # plain reply path.
            reply_text = self._stringify(agent_stream)
            if reply_text:
                await self._send_markdown(
                    chat_id=chat_id,
                    message_id=anchor,
                    uuid_message_id=message_id,
                    text=reply_text,
                    is_group=is_group,
                )
            return

        async def producer(stream):
            async for chunk in agent_stream:  # type: ignore[func-returns-value]
                if chunk:
                    await stream.append(chunk)

        opts = self._send_opts(
            message_id=anchor,
            is_group=is_group,
            uuid_message_id=message_id,
        )
        assert self._channel is not None
        result = await self._channel.stream(chat_id, {"markdown": producer}, opts)
        self._log_send_result(result=result, chat_id=chat_id, message_id=anchor)

    # ------------------------------------------------------------------
    # Outbound helpers
    # ------------------------------------------------------------------

    def _send_opts(
        self,
        *,
        message_id: Optional[str],
        is_group: bool,
        uuid_message_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        opts: dict[str, Any] = {}
        # group/topic: anchor reply to the source message, but never as a
        # Feishu topic/thread reply.
        if message_id and is_group:
            opts["reply_to"] = message_id
        uuid = self._message_uuid(uuid_message_id or message_id)
        if uuid:
            opts["uuid"] = uuid
        return opts or None

    @staticmethod
    def _message_uuid(message_id: Optional[str]) -> Optional[str]:
        if not isinstance(message_id, str):
            return None
        normalized = message_id.strip()
        if not normalized:
            return None
        if len(normalized) <= 50:
            return normalized
        return hashlib.sha1(normalized.encode("utf-8")).hexdigest()

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
        chat_type = self._message_field(raw_msg, "chat_type")
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
            value = FeishuAdapter._message_field(msg, attr)
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
        uuid_message_id: Optional[str] = None,
    ) -> None:
        assert self._channel is not None
        try:
            await send_message(
                self._channel,
                chat_id=chat_id,
                payload={"markdown": text},
                reply_to=message_id if (is_group and message_id) else None,
                uuid=self._message_uuid(uuid_message_id or message_id),
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
