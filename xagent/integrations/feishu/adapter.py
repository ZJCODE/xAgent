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
from typing import Any, AsyncGenerator, Optional, Union

from pydantic import BaseModel

from ...core.agent import Agent
from .config import FeishuAdapterConfig
from .history import FeishuHistoryFetcher, format_context_recap


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
        channel = self._channel
        if channel is None:
            return
        try:
            channel.stop()
        except Exception:  # pragma: no cover - best-effort cleanup
            self.logger.debug("FeishuChannel stop raised", exc_info=True)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    async def _on_message(self, msg: Any) -> None:
        try:
            await self._dispatch(msg)
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

    async def _prime_context(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        raw_msg: Any,
        is_group: bool,
        user_id: str,
    ) -> None:
        fetcher = self._get_history_fetcher()
        if fetcher is None:
            return

        parent_id = self._reply_to_message_id(raw_msg)
        thread_id = self._thread_id(raw_msg) if is_group else None
        history_count = self.config.chat_history_count if is_group else 0

        if not parent_id and not thread_id and history_count <= 0:
            return

        try:
            # Hard cap the prefetch so a slow / unauthorized Feishu API
            # never blocks the @-mention reply.
            records = await asyncio.wait_for(
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
            return
        except Exception:
            self.logger.exception("Feishu context prefetch failed; continuing without it")
            return

        if not records:
            self.logger.debug(
                "Feishu context prefetch returned no records (chat_id=%s, parent=%s, thread=%s, n=%d)",
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
        await self._send_markdown(
            chat_id=chat_id,
            message_id=message_id,
            text=reply_text,
            is_group=is_group,
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
    ) -> None:
        agent_stream = await self.agent.chat(**chat_kwargs)
        if not hasattr(agent_stream, "__aiter__"):
            # Agent fell back to non-stream (e.g. structured output); use the
            # plain reply path.
            reply_text = self._stringify(agent_stream)
            if reply_text:
                await self._send_markdown(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=reply_text,
                    is_group=is_group,
                )
            return

        async def producer(stream):
            async for chunk in agent_stream:  # type: ignore[func-returns-value]
                if chunk:
                    await stream.append(chunk)

        opts = self._send_opts(message_id=message_id, is_group=is_group)
        assert self._channel is not None
        result = await self._channel.stream(chat_id, {"markdown": producer}, opts)
        self._log_send_result(result=result, chat_id=chat_id, message_id=message_id)

    # ------------------------------------------------------------------
    # Observe path
    # ------------------------------------------------------------------

    async def _handle_observe(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        sender_id: str,
        text: str,
    ) -> None:
        observe_kwargs: dict[str, Any] = {
            "context": text,
            "current_user_id": sender_id,
            "source": "feishu",
            "event_type": "group_message",
            "sender_id": sender_id,
            "metadata": {
                "chat_id": chat_id,
                "message_id": message_id,
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

    async def _send_markdown(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        text: str,
        is_group: bool,
    ) -> None:
        assert self._channel is not None
        opts = self._send_opts(message_id=message_id, is_group=is_group)
        try:
            result = await self._channel.send(chat_id, {"markdown": text}, opts)
            self._log_send_result(result=result, chat_id=chat_id, message_id=message_id)
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
