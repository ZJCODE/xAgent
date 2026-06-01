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
import base64
import hashlib
import inspect
import json
import logging
import mimetypes
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncGenerator, Optional, Union
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import AsyncTaskScheduler, ScheduledDeliveryContext, scheduled_delivery_context
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
from ...utils.image_utils import (
    DEFAULT_IMAGE_TRANSPORT_MAX_BYTES,
    DEFAULT_IMAGE_TRANSPORT_MAX_EDGE,
    compress_image_bytes_for_transport,
    workspace_blob_url,
)
from ...schemas.attachment import (
    ATTACHMENT_KIND_IMAGE,
    DEFAULT_FEISHU_ATTACHMENT_DIR,
    attachment_kind,
    attachment_markdown,
    save_workspace_attachment_bytes,
    workspace_attachment_from_path,
)


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

_FEISHU_IMAGE_PLACEHOLDER = "The user sent an image."
_FEISHU_INBOUND_IMAGE_OUTPUT_DIR = "temp/images/feishu"
_FEISHU_OUTBOUND_IMAGE_OUTPUT_DIR = "temp/images/feishu/outbound"
_FEISHU_IMAGE_TRANSPORT_MAX_BYTES = DEFAULT_IMAGE_TRANSPORT_MAX_BYTES
_FEISHU_IMAGE_TRANSPORT_MAX_EDGE = DEFAULT_IMAGE_TRANSPORT_MAX_EDGE
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class _FeishuOutboundAttachment:
    kind: str
    path: Path
    caption: str = ""
    blob_url: str = ""


@dataclass(frozen=True)
class _FeishuImageResource:
    data: bytes
    mime_type: str
    file_name: str


@dataclass(frozen=True)
class _FeishuFileResource:
    data: bytes
    mime_type: str
    file_name: str


@dataclass(frozen=True)
class _FeishuInboundImageAsset:
    image_source: str
    path: str = ""
    blob_url: str = ""
    markdown: str = ""


@dataclass(frozen=True)
class _FeishuInboundAttachmentAsset:
    attachment: dict[str, Any]
    image_source: str = ""
    markdown: str = ""


@dataclass(frozen=True)
class _FeishuInboundImageDownloadFailure:
    resource_type: str
    file_key: str
    file_name: str = ""
    reason: str = ""


@dataclass(frozen=True)
class _FeishuInboundImageDownloadResult:
    assets: list[_FeishuInboundImageAsset]
    failed_resources: list[_FeishuInboundImageDownloadFailure]


@dataclass(frozen=True)
class _FeishuInboundAttachmentDownloadResult:
    assets: list[_FeishuInboundAttachmentAsset]
    failed_resources: list[_FeishuInboundImageDownloadFailure]


class FeishuArtifactRenderer:
    """Render structured workspace artifacts to Feishu messages."""

    def __init__(self, adapter: "FeishuAdapter", *, mode: str = "separate") -> None:
        self.adapter = adapter
        self.mode = mode

    async def send(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid_message_id: Optional[str],
        attachments: list[_FeishuOutboundAttachment],
        is_group: bool,
    ) -> None:
        await self.adapter._send_outbound_attachments_separate(
            chat_id=chat_id,
            message_id=message_id,
            uuid_message_id=uuid_message_id,
            attachments=attachments,
            is_group=is_group,
        )


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
        self._artifact_renderer = FeishuArtifactRenderer(self)
        runtime_root = Path(getattr(agent, "workspace", AgentConfig.DEFAULT_WORKSPACE)).expanduser().resolve()
        self._tasks_dir = runtime_root / AgentConfig.TASKS_DIRNAME
        self._task_scheduler: Optional[AsyncTaskScheduler] = None

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
        task_scheduler = AsyncTaskScheduler(
            self._tasks_dir,
            can_handle=self._can_handle_scheduled_task,
            dispatch=self._dispatch_scheduled_task,
            logger_=self.logger,
        )
        self._task_scheduler = task_scheduler
        await task_scheduler.start()
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
            await task_scheduler.stop()
            self._task_scheduler = None
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

    def _on_reconnecting(self, *_: Any) -> None:
        self.logger.warning("FeishuChannel reconnecting…")

    def _on_reconnected(self, *_: Any) -> None:
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
            attachment_download = await self._download_message_attachment_assets_with_failures(msg, message_id=message_id)
            if attachment_download.failed_resources:
                await self._send_attachment_download_failed(
                    chat_id=chat_id,
                    message_id=message_id,
                    raw_msg=msg,
                    is_group=False,
                    failures=attachment_download.failed_resources,
                )
                return
            attachment_assets = attachment_download.assets
            image_assets = self._image_assets_from_attachment_assets(attachment_assets)
            if not text and not attachment_assets:
                self.logger.debug("Skipping non-text direct message")
                return
            if not text and image_assets and len(image_assets) == len(attachment_assets):
                text = _FEISHU_IMAGE_PLACEHOLDER
            elif not text:
                text = "The user sent file attachments."
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
                image_assets=image_assets,
                attachments=self._attachments_from_attachment_assets(attachment_assets),
            )
            return

        if chat_type in {"group", "topic"}:
            if self._is_bot_mentioned(msg):
                attachment_download = await self._download_message_attachment_assets_with_failures(msg, message_id=message_id)
                if attachment_download.failed_resources:
                    await self._send_attachment_download_failed(
                        chat_id=chat_id,
                        message_id=message_id,
                        raw_msg=msg,
                        is_group=True,
                        failures=attachment_download.failed_resources,
                    )
                    return
                attachment_assets = attachment_download.assets
                image_assets = self._image_assets_from_attachment_assets(attachment_assets)
                if not text and image_assets and len(image_assets) == len(attachment_assets):
                    text = _FEISHU_IMAGE_PLACEHOLDER
                elif not text and attachment_assets:
                    text = "The user mentioned you with file attachments."
                elif not text:
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
                    image_assets=image_assets,
                    attachments=self._attachments_from_attachment_assets(attachment_assets),
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

    async def _download_message_image_assets(
        self,
        msg: Any,
        *,
        message_id: Optional[str],
    ) -> list[_FeishuInboundImageAsset]:
        result = await self._download_message_image_assets_with_failures(msg, message_id=message_id)
        return result.assets

    async def _download_message_attachment_assets_with_failures(
        self,
        msg: Any,
        *,
        message_id: Optional[str],
    ) -> _FeishuInboundAttachmentDownloadResult:
        resources = self._message_attachment_resources(msg)
        if not resources:
            return _FeishuInboundAttachmentDownloadResult(assets=[], failed_resources=[])
        if not message_id:
            return _FeishuInboundAttachmentDownloadResult(
                assets=[],
                failed_resources=[
                    _FeishuInboundImageDownloadFailure(
                        resource_type=resource_type,
                        file_key=file_key,
                        file_name=file_name,
                        reason="missing message_id",
                    )
                    for resource_type, file_key, file_name in resources
                ],
            )

        client = getattr(self._channel, "client", None)
        resource_api = getattr(getattr(getattr(client, "im", None), "v1", None), "message_resource", None)
        request_cls = None
        if resource_api is not None:
            try:
                from lark_oapi.api.im.v1 import GetMessageResourceRequest  # type: ignore

                request_cls = GetMessageResourceRequest
            except ImportError:  # pragma: no cover - import guard
                request_cls = None

        assets: list[_FeishuInboundAttachmentAsset] = []
        failed_resources: list[_FeishuInboundImageDownloadFailure] = []
        for resource_type, file_key, file_name in resources:
            resource = await self._download_file_resource_via_channel(
                resource_type=resource_type,
                file_key=file_key,
                file_name=file_name,
                message_id=message_id,
            )
            fallback_reason = ""
            if resource is None:
                if resource_api is None or request_cls is None:
                    fallback_reason = "message_resource API unavailable"
                else:
                    request = (
                        request_cls.builder()
                        .message_id(message_id)
                        .file_key(file_key)
                        .type(resource_type)
                        .build()
                    )
                    try:
                        response = await self._call_feishu_resource_get(resource_api, request)
                    except Exception as exc:
                        fallback_reason = str(exc)
                        self.logger.info(
                            "Feishu resource download failed: message_id=%s file_key=%s resource_type=%s error=%s",
                            message_id,
                            file_key,
                            resource_type,
                            exc,
                        )
                    else:
                        if not self._feishu_response_success(response):
                            fallback_reason = f"code={getattr(response, 'code', None)} msg={getattr(response, 'msg', None)}"
                            self.logger.info(
                                "Feishu resource download rejected: message_id=%s file_key=%s resource_type=%s code=%s msg=%s",
                                message_id,
                                file_key,
                                resource_type,
                                getattr(response, "code", None),
                                getattr(response, "msg", None),
                            )
                        else:
                            resource = self._feishu_resource_to_file(response, fallback_name=file_name or file_key)

            if resource is None:
                failed_resources.append(_FeishuInboundImageDownloadFailure(
                    resource_type=resource_type,
                    file_key=file_key,
                    file_name=file_name,
                    reason=fallback_reason or "resource did not contain file bytes",
                ))
                self.logger.info(
                    "Feishu resource unavailable: message_id=%s file_key=%s resource_type=%s reason=%s",
                    message_id,
                    file_key,
                    resource_type,
                    fallback_reason or "resource did not contain file bytes",
                )
                continue

            asset = self._save_feishu_inbound_attachment(
                resource,
                message_id=message_id,
                resource_type=resource_type,
                file_key=file_key,
            )
            if asset is not None:
                assets.append(asset)
                self.logger.debug(
                    "Feishu resource saved: message_id=%s file_key=%s path=%s",
                    message_id,
                    file_key,
                    asset.attachment.get("path"),
                )
                continue

            if resource.mime_type.startswith("image/"):
                image_data = self._image_resource_to_data_uri(_FeishuImageResource(
                    data=resource.data,
                    mime_type=resource.mime_type,
                    file_name=resource.file_name,
                ))
                if image_data:
                    assets.append(_FeishuInboundAttachmentAsset(attachment={}, image_source=image_data))
                    continue
            failed_resources.append(_FeishuInboundImageDownloadFailure(
                resource_type=resource_type,
                file_key=file_key,
                file_name=file_name,
                reason="failed to save attachment",
            ))
        return _FeishuInboundAttachmentDownloadResult(assets=assets, failed_resources=failed_resources)

    async def _download_message_image_assets_with_failures(
        self,
        msg: Any,
        *,
        message_id: Optional[str],
    ) -> _FeishuInboundImageDownloadResult:
        resources = self._message_image_resources(msg)
        if not resources:
            return _FeishuInboundImageDownloadResult(assets=[], failed_resources=[])
        if not message_id:
            return _FeishuInboundImageDownloadResult(
                assets=[],
                failed_resources=[
                    _FeishuInboundImageDownloadFailure(
                        resource_type=resource_type,
                        file_key=file_key,
                        file_name=file_name,
                        reason="missing message_id",
                    )
                    for resource_type, file_key, file_name in resources
                ],
            )

        client = getattr(self._channel, "client", None)
        im_v1 = getattr(getattr(getattr(client, "im", None), "v1", None), "message_resource", None)

        request_cls = None
        if im_v1 is not None:
            try:
                from lark_oapi.api.im.v1 import GetMessageResourceRequest  # type: ignore

                request_cls = GetMessageResourceRequest
            except ImportError:  # pragma: no cover - import guard
                request_cls = None

        image_assets: list[_FeishuInboundImageAsset] = []
        failed_resources: list[_FeishuInboundImageDownloadFailure] = []
        for resource_type, file_key, file_name in resources:
            image_resource = await self._download_image_resource_via_channel(
                resource_type=resource_type,
                file_key=file_key,
                file_name=file_name,
                message_id=message_id,
            )
            fallback_reason = ""
            if image_resource is None:
                if im_v1 is None or request_cls is None:
                    fallback_reason = "message_resource API unavailable"
                else:
                    request = (
                        request_cls.builder()
                        .message_id(message_id)
                        .file_key(file_key)
                        .type(resource_type)
                        .build()
                    )
                    try:
                        response = await self._call_feishu_resource_get(im_v1, request)
                    except Exception as exc:
                        fallback_reason = str(exc)
                        self.logger.info(
                            "Feishu image resource download failed: message_id=%s file_key=%s resource_type=%s error=%s",
                            message_id,
                            file_key,
                            resource_type,
                            exc,
                        )
                    else:
                        if not self._feishu_response_success(response):
                            fallback_reason = f"code={getattr(response, 'code', None)} msg={getattr(response, 'msg', None)}"
                            self.logger.info(
                                "Feishu image resource download rejected: message_id=%s file_key=%s resource_type=%s code=%s msg=%s",
                                message_id,
                                file_key,
                                resource_type,
                                getattr(response, "code", None),
                                getattr(response, "msg", None),
                            )
                        else:
                            image_resource = self._feishu_resource_to_image(response, fallback_name=file_name or file_key)

            if image_resource is None:
                failed_resources.append(_FeishuInboundImageDownloadFailure(
                    resource_type=resource_type,
                    file_key=file_key,
                    file_name=file_name,
                    reason=fallback_reason or "resource did not contain image bytes",
                ))
                self.logger.info(
                    "Feishu image resource unavailable: message_id=%s file_key=%s resource_type=%s reason=%s",
                    message_id,
                    file_key,
                    resource_type,
                    fallback_reason or "resource did not contain image bytes",
                )
                continue

            asset = self._save_feishu_inbound_image(
                image_resource,
                message_id=message_id,
                resource_type=resource_type,
                file_key=file_key,
            )
            if asset is not None:
                image_assets.append(asset)
                self.logger.debug(
                    "Feishu image resource saved: message_id=%s file_key=%s path=%s",
                    message_id,
                    file_key,
                    asset.path,
                )
                continue
            image_data = self._image_resource_to_data_uri(image_resource)
            if image_data:
                image_assets.append(_FeishuInboundImageAsset(image_source=image_data))
                continue
            failed_resources.append(_FeishuInboundImageDownloadFailure(
                resource_type=resource_type,
                file_key=file_key,
                file_name=file_name,
                reason="failed to create model image source",
            ))
        return _FeishuInboundImageDownloadResult(assets=image_assets, failed_resources=failed_resources)

    async def _download_image_resource_via_channel(
        self,
        *,
        resource_type: str,
        file_key: str,
        file_name: str,
        message_id: str,
    ) -> Optional[_FeishuImageResource]:
        downloader = getattr(self._channel, "download_resource", None)
        if not callable(downloader):
            return None

        call_variants = (
            lambda: downloader(
                file_key=file_key,
                resource_type=resource_type,
                message_id=message_id,
            ),
            lambda: downloader(
                file_key=file_key,
                type=resource_type,
                message_id=message_id,
            ),
            lambda: downloader(file_key, resource_type=resource_type, message_id=message_id),
            lambda: downloader(file_key, message_id=message_id),
            lambda: downloader(file_key),
        )
        last_type_error: Optional[TypeError] = None
        for build_call in call_variants:
            try:
                result = build_call()
            except TypeError as exc:
                last_type_error = exc
                continue
            try:
                result = await result if inspect.isawaitable(result) else result
            except Exception as exc:
                self.logger.info(
                    "Feishu public resource download failed: message_id=%s file_key=%s resource_type=%s error=%s",
                    message_id,
                    file_key,
                    resource_type,
                    exc,
                )
                return None
            image = self._download_result_to_image_resource(result, fallback_name=file_name or file_key)
            if image is not None:
                return image
            return None
        if last_type_error is not None:
            self.logger.debug(
                "Feishu public resource downloader signature did not match: message_id=%s file_key=%s resource_type=%s error=%s",
                message_id,
                file_key,
                resource_type,
                last_type_error,
            )
        return None

    async def _download_file_resource_via_channel(
        self,
        *,
        resource_type: str,
        file_key: str,
        file_name: str,
        message_id: str,
    ) -> Optional[_FeishuFileResource]:
        downloader = getattr(self._channel, "download_resource", None)
        if not callable(downloader):
            return None

        call_variants = (
            lambda: downloader(
                file_key=file_key,
                resource_type=resource_type,
                message_id=message_id,
            ),
            lambda: downloader(
                file_key=file_key,
                type=resource_type,
                message_id=message_id,
            ),
            lambda: downloader(file_key, resource_type=resource_type, message_id=message_id),
            lambda: downloader(file_key, message_id=message_id),
            lambda: downloader(file_key),
        )
        last_type_error: Optional[TypeError] = None
        for build_call in call_variants:
            try:
                result = build_call()
            except TypeError as exc:
                last_type_error = exc
                continue
            try:
                result = await result if inspect.isawaitable(result) else result
            except Exception as exc:
                self.logger.info(
                    "Feishu public resource download failed: message_id=%s file_key=%s resource_type=%s error=%s",
                    message_id,
                    file_key,
                    resource_type,
                    exc,
                )
                return None
            return self._download_result_to_file_resource(result, fallback_name=file_name or file_key)
        if last_type_error is not None:
            self.logger.debug(
                "Feishu public resource downloader signature did not match: message_id=%s file_key=%s resource_type=%s error=%s",
                message_id,
                file_key,
                resource_type,
                last_type_error,
            )
        return None

    @classmethod
    def _download_result_to_image_resource(
        cls,
        result: Any,
        *,
        fallback_name: str,
    ) -> Optional[_FeishuImageResource]:
        if isinstance(result, _FeishuImageResource):
            return result
        if result is None:
            return None
        if getattr(result, "success", True) is False:
            return None

        data: Any = None
        file_name = fallback_name
        mime_type = ""
        if isinstance(result, bytes):
            data = result
        elif isinstance(result, dict):
            data = result.get("data") or result.get("bytes") or result.get("content")
            file_name = str(result.get("file_name") or result.get("name") or fallback_name)
            mime_type = str(result.get("mime_type") or result.get("content_type") or "")
        elif isinstance(result, (str, Path)):
            path = Path(result).expanduser()
            if path.is_file():
                data = path.read_bytes()
                file_name = path.name
                guessed, _ = mimetypes.guess_type(path.name)
                mime_type = guessed or ""
        else:
            data = (
                getattr(result, "data", None)
                or getattr(result, "bytes", None)
                or getattr(result, "content", None)
            )
            file_name = str(getattr(result, "file_name", None) or getattr(result, "name", None) or fallback_name)
            mime_type = str(getattr(result, "mime_type", None) or getattr(result, "content_type", None) or "")
            file_obj = getattr(result, "file", None)
            if data is None and file_obj is not None:
                try:
                    data = file_obj.getvalue() if hasattr(file_obj, "getvalue") else file_obj.read()
                except Exception:
                    data = None

        if isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes) or not data:
            return None
        detected_mime_type = cls._detect_image_mime(data)
        mime_type = detected_mime_type or mime_type.split(";", 1)[0].strip().lower()
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_name)
        if not mime_type or not mime_type.startswith("image/"):
            return None
        return _FeishuImageResource(data=data, mime_type=mime_type, file_name=file_name)

    @classmethod
    def _download_result_to_file_resource(
        cls,
        result: Any,
        *,
        fallback_name: str,
    ) -> Optional[_FeishuFileResource]:
        if isinstance(result, _FeishuFileResource):
            return result
        if isinstance(result, _FeishuImageResource):
            return _FeishuFileResource(data=result.data, mime_type=result.mime_type, file_name=result.file_name)
        if result is None or getattr(result, "success", True) is False:
            return None

        data: Any = None
        file_name = fallback_name
        mime_type = ""
        if isinstance(result, bytes):
            data = result
        elif isinstance(result, dict):
            data = result.get("data") or result.get("bytes") or result.get("content")
            file_name = str(result.get("file_name") or result.get("name") or fallback_name)
            mime_type = str(result.get("mime_type") or result.get("content_type") or "")
        elif isinstance(result, (str, Path)):
            path = Path(result).expanduser()
            if path.is_file():
                data = path.read_bytes()
                file_name = path.name
                guessed, _ = mimetypes.guess_type(path.name)
                mime_type = guessed or ""
        else:
            data = (
                getattr(result, "data", None)
                or getattr(result, "bytes", None)
                or getattr(result, "content", None)
            )
            file_name = str(getattr(result, "file_name", None) or getattr(result, "name", None) or fallback_name)
            mime_type = str(getattr(result, "mime_type", None) or getattr(result, "content_type", None) or "")
            file_obj = getattr(result, "file", None)
            if data is None and file_obj is not None:
                try:
                    data = file_obj.getvalue() if hasattr(file_obj, "getvalue") else file_obj.read()
                except Exception:
                    data = None

        if isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes) or not data:
            return None
        detected_mime_type = cls._detect_image_mime(data)
        normalized_mime_type = detected_mime_type or str(mime_type or "").split(";", 1)[0].strip().lower()
        if not normalized_mime_type:
            normalized_mime_type, _ = mimetypes.guess_type(file_name)
        return _FeishuFileResource(
            data=data,
            mime_type=normalized_mime_type or "application/octet-stream",
            file_name=file_name or fallback_name or "attachment.bin",
        )

    @staticmethod
    async def _call_feishu_resource_get(resource_api: Any, request: Any) -> Any:
        getter = getattr(resource_api, "aget", None)
        if callable(getter):
            response = getter(request)
            return await response if inspect.isawaitable(response) else response
        getter = getattr(resource_api, "get", None)
        if not callable(getter):
            raise RuntimeError("message_resource.get is unavailable")
        return await asyncio.to_thread(getter, request)

    @classmethod
    def _message_image_resources(cls, msg: Any) -> list[tuple[str, str, str]]:
        resources: list[tuple[str, str, str]] = []
        for resource_type, file_key, file_name in cls._message_attachment_resources(msg):
            if resource_type == "image" or cls._looks_like_image_file(file_name):
                resources.append((resource_type, file_key, file_name))
        return resources

    @classmethod
    def _message_attachment_resources(cls, msg: Any) -> list[tuple[str, str, str]]:
        payloads: list[Any] = []
        message = cls._message_object(msg) or msg
        for source in (
            cls._message_field(msg, "content"),
            cls._raw_field(cls._raw_field(message, "body"), "content"),
            cls._message_field(msg, "image_key"),
            cls._message_field(msg, "file_key"),
        ):
            payload = cls._parse_message_payload(source)
            if payload not in (None, ""):
                payloads.append(payload)

        resources: list[tuple[str, str, str]] = []
        seen: set[tuple[str, str]] = set()

        def add_resource(resource_type: str, file_key: str, file_name: str = "") -> None:
            key_value = str(file_key or "").strip()
            if not key_value:
                return
            key = (resource_type, key_value)
            if key in seen:
                return
            seen.add(key)
            resources.append((resource_type, key_value, file_name))

        for payload in payloads:
            for resource_type, file_key, file_name in cls._extract_attachment_resource_items(payload):
                add_resource(resource_type, file_key, file_name)

        direct_image_key = cls._message_field(msg, "image_key")
        if isinstance(direct_image_key, str):
            add_resource("image", direct_image_key, cls._resource_file_name_from_message(msg))
        direct_file_key = cls._message_field(msg, "file_key")
        if isinstance(direct_file_key, str):
            add_resource("file", direct_file_key, cls._resource_file_name_from_message(msg))
        return resources

    @classmethod
    def _parse_message_payload(cls, value: Any) -> Any:
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return ""
            try:
                return json.loads(stripped)
            except (TypeError, ValueError):
                return stripped
        return value

    @classmethod
    def _extract_image_resource_items(cls, value: Any) -> list[tuple[str, str, str]]:
        return [
            item
            for item in cls._extract_attachment_resource_items(value)
            if item[0] == "image" or cls._looks_like_image_file(item[2])
        ]

    @classmethod
    def _extract_attachment_resource_items(cls, value: Any) -> list[tuple[str, str, str]]:
        items: list[tuple[str, str, str]] = []

        def visit(node: Any) -> None:
            if isinstance(node, str):
                if node.startswith("img_"):
                    items.append(("image", node, ""))
                return
            if isinstance(node, dict):
                image_key = node.get("image_key") or node.get("imageKey")
                if isinstance(image_key, str) and image_key.strip():
                    items.append(("image", image_key.strip(), cls._resource_file_name(node)))
                file_key = node.get("file_key") or node.get("fileKey")
                if isinstance(file_key, str) and file_key.strip():
                    file_name = cls._resource_file_name(node)
                    items.append(("file", file_key.strip(), file_name))
                for child in node.values():
                    visit(child)
                return
            if isinstance(node, (list, tuple)):
                for child in node:
                    visit(child)
                return
            attrs = getattr(node, "__dict__", None)
            if isinstance(attrs, dict):
                visit(attrs)

        visit(value)
        return items

    @staticmethod
    def _resource_file_name(payload: dict[str, Any]) -> str:
        value = payload.get("file_name") or payload.get("fileName") or payload.get("name") or ""
        return value.strip() if isinstance(value, str) else ""

    @classmethod
    def _resource_file_name_from_message(cls, msg: Any) -> str:
        for field_name in ("file_name", "fileName", "name"):
            value = cls._message_field(msg, field_name)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _looks_like_image_file(file_name: str) -> bool:
        if not file_name:
            return False
        mime_type, _ = mimetypes.guess_type(file_name)
        return bool(mime_type and mime_type.startswith("image/"))

    @staticmethod
    def _feishu_response_success(response: Any) -> bool:
        success = getattr(response, "success", None)
        if callable(success):
            return bool(success())
        return getattr(response, "code", None) in (0, "0")

    @classmethod
    def _feishu_resource_to_image(cls, response: Any, *, fallback_name: str) -> Optional[_FeishuImageResource]:
        file_obj = getattr(response, "file", None)
        if file_obj is None:
            return None
        try:
            if hasattr(file_obj, "getvalue"):
                data = file_obj.getvalue()
            else:
                data = file_obj.read()
        except Exception:
            return None
        if not data:
            return None

        file_name = str(getattr(response, "file_name", None) or fallback_name or "")
        detected_mime_type = cls._detect_image_mime(data)
        mime_type = detected_mime_type or cls._response_content_type(response)
        if not mime_type or not mime_type.startswith("image/"):
            mime_type, _ = mimetypes.guess_type(file_name)
        if not mime_type:
            return None
        return _FeishuImageResource(data=data, mime_type=mime_type, file_name=file_name)

    @classmethod
    def _feishu_resource_to_file(cls, response: Any, *, fallback_name: str) -> Optional[_FeishuFileResource]:
        file_obj = getattr(response, "file", None)
        if file_obj is None:
            return None
        try:
            if hasattr(file_obj, "getvalue"):
                data = file_obj.getvalue()
            else:
                data = file_obj.read()
        except Exception:
            return None
        if isinstance(data, bytearray):
            data = bytes(data)
        if not isinstance(data, bytes) or not data:
            return None

        file_name = str(getattr(response, "file_name", None) or fallback_name or "attachment.bin")
        detected_mime_type = cls._detect_image_mime(data)
        mime_type = detected_mime_type or cls._response_content_type(response)
        if not mime_type:
            mime_type, _ = mimetypes.guess_type(file_name)
        return _FeishuFileResource(
            data=data,
            mime_type=mime_type or "application/octet-stream",
            file_name=file_name,
        )

    def _save_feishu_inbound_image(
        self,
        image: _FeishuImageResource,
        *,
        message_id: str,
        resource_type: str,
        file_key: str,
    ) -> Optional[_FeishuInboundImageAsset]:
        workspace_root = self._agent_workspace_root()
        if workspace_root is None:
            return None
        image = self._compress_feishu_image_resource(
            image,
            direction="inbound",
            message_id=message_id,
            resource_type=resource_type,
            file_key=file_key,
        )
        output_dir = workspace_root / _FEISHU_INBOUND_IMAGE_OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)

        stem = self._safe_filename_part(Path(image.file_name).stem or file_key or resource_type)
        resource_hash = hashlib.sha1(f"{message_id}:{resource_type}:{file_key}".encode("utf-8")).hexdigest()[:12]
        content_hash = hashlib.sha256(image.data).hexdigest()[:12]
        extension = self._image_extension(image.mime_type, image.file_name)
        output_path = (output_dir / f"{stem}-{resource_hash}-{content_hash}.{extension}").resolve()
        if not output_path.is_relative_to(workspace_root):
            return None
        if not output_path.exists():
            output_path.write_bytes(image.data)
        relative_path = output_path.relative_to(workspace_root).as_posix()
        blob_url = workspace_blob_url(relative_path)
        return _FeishuInboundImageAsset(
            image_source=blob_url,
            path=relative_path,
            blob_url=blob_url,
            markdown=f"![Feishu image]({blob_url})",
        )

    def _compress_feishu_image_resource(
        self,
        image: _FeishuImageResource,
        *,
        direction: str,
        message_id: str = "",
        resource_type: str = "",
        file_key: str = "",
        path: Optional[Path] = None,
    ) -> _FeishuImageResource:
        compressed = compress_image_bytes_for_transport(
            image.data,
            mime_type=image.mime_type,
            file_name=image.file_name,
            max_bytes=_FEISHU_IMAGE_TRANSPORT_MAX_BYTES,
            max_edge=_FEISHU_IMAGE_TRANSPORT_MAX_EDGE,
        )
        if not compressed.compressed:
            return image
        self.logger.info(
            "Feishu %s image compressed: path=%s message_id=%s resource_type=%s file_key=%s original_bytes=%s compressed_bytes=%s size=%sx%s",
            direction,
            str(path) if path is not None else "",
            message_id,
            resource_type,
            file_key,
            compressed.original_size,
            len(compressed.data),
            compressed.width or "?",
            compressed.height or "?",
        )
        return _FeishuImageResource(
            data=compressed.data,
            mime_type=compressed.mime_type,
            file_name=compressed.file_name,
        )

    def _save_feishu_inbound_attachment(
        self,
        resource: _FeishuFileResource,
        *,
        message_id: str,
        resource_type: str,
        file_key: str,
    ) -> Optional[_FeishuInboundAttachmentAsset]:
        workspace_root = self._agent_workspace_root()
        if workspace_root is None:
            return None
        if attachment_kind(resource.mime_type, resource.file_name) == ATTACHMENT_KIND_IMAGE:
            image_asset = self._save_feishu_inbound_image(
                _FeishuImageResource(
                    data=resource.data,
                    mime_type=resource.mime_type,
                    file_name=resource.file_name,
                ),
                message_id=message_id,
                resource_type=resource_type,
                file_key=file_key,
            )
            if image_asset is None or not image_asset.path:
                return None
            attachment = workspace_attachment_from_path(
                workspace_root / image_asset.path,
                workspace_root,
                caption="Feishu image",
                source_channel="feishu",
                source_message_id=message_id,
                source_resource_id=file_key,
                source_resource_type=resource_type,
            )
            return _FeishuInboundAttachmentAsset(
                attachment=attachment,
                image_source=image_asset.image_source,
                markdown=image_asset.markdown,
            )

        attachment = save_workspace_attachment_bytes(
            resource.data,
            workspace_root,
            directory=DEFAULT_FEISHU_ATTACHMENT_DIR,
            file_name=resource.file_name or file_key or "attachment.bin",
            mime_type=resource.mime_type,
            source_channel="feishu",
            source_message_id=message_id,
            source_resource_id=file_key,
            source_resource_type=resource_type,
        )
        return _FeishuInboundAttachmentAsset(
            attachment=attachment,
            markdown=attachment_markdown(attachment),
        )

    def _agent_workspace_root(self) -> Optional[Path]:
        workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            return None
        return Path(workspace_dir).expanduser().resolve()

    @staticmethod
    def _safe_filename_part(value: str) -> str:
        safe_value = _SAFE_FILENAME_RE.sub("-", value.strip()).strip(".-_")
        return (safe_value[:48] or "feishu-image")

    @staticmethod
    def _image_extension(mime_type: str, file_name: str = "") -> str:
        extension = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
            "image/bmp": "bmp",
            "image/tiff": "tiff",
            "image/svg+xml": "svg",
        }.get(mime_type.lower())
        if extension:
            return extension
        guessed_from_name = Path(file_name).suffix.lower().lstrip(".")
        if guessed_from_name in {"png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff", "svg"}:
            return "jpg" if guessed_from_name == "jpeg" else guessed_from_name
        return "png"

    @staticmethod
    def _image_resource_to_data_uri(image: _FeishuImageResource) -> str:
        encoded = base64.b64encode(image.data).decode("ascii")
        return f"data:{image.mime_type};base64,{encoded}"

    @classmethod
    def _feishu_resource_to_data_uri(cls, response: Any, *, fallback_name: str) -> str:
        image = cls._feishu_resource_to_image(response, fallback_name=fallback_name)
        return cls._image_resource_to_data_uri(image) if image is not None else ""

    @staticmethod
    def _response_content_type(response: Any) -> str:
        raw = getattr(response, "raw", None)
        headers = getattr(raw, "headers", None) or {}
        if not isinstance(headers, dict):
            return ""
        content_type = headers.get("content-type") or headers.get("Content-Type") or ""
        return str(content_type).split(";", 1)[0].strip().lower()

    @staticmethod
    def _detect_image_mime(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return "image/webp"
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
        image_assets: Optional[list[_FeishuInboundImageAsset]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        image_assets = image_assets or []
        supports_vision = bool(getattr(self.agent, "supports_vision", True))
        image_sources = self._image_sources_for_model(image_assets) if supports_vision else []

        chat_text = self._append_image_markdown_context(text, image_assets)
        if is_group:
            chat_text = await self._chat_text_with_group_history(
                chat_id=chat_id,
                current_message_id=message_id,
                raw_msg=raw_msg,
                sender_id=sender_id,
                sender_name=sender_name,
                text=chat_text,
            )

        chat_kwargs = self._chat_kwargs(
            user_id=user_id,
            text=chat_text,
            image_sources=image_sources,
            attachments=attachments,
        )
        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        context = ScheduledDeliveryContext(
            channel="feishu",
            user_id=user_id,
            target={
                "chat_id": chat_id,
                "message_id": anchor,
                "is_group": is_group,
                "sender_id": sender_id,
                "sender_name": sender_name,
            },
            metadata={
                "source": "feishu",
                "message_id": message_id,
                "chat_id": chat_id,
            },
        )
        with scheduled_delivery_context(context):
            await self._send_event_replies(
                chat_id=chat_id,
                is_group=is_group,
                message_id=message_id,
                chat_kwargs=chat_kwargs,
                raw_msg=raw_msg,
            )

    def _can_handle_scheduled_task(self, task) -> bool:
        return (
            task.kind == "task"
            and task.delivery_channel == "feishu"
            and self._channel is not None
        )

    async def _dispatch_scheduled_task(self, task) -> None:
        assert self._channel is not None
        target = task.target
        chat_id = str(target.get("chat_id") or "").strip()
        if not chat_id:
            raise ValueError("scheduled Feishu task is missing chat_id")
        content = await self._scheduled_task_result(task)
        if not content:
            raise ValueError("scheduled Feishu task produced no content")
        message_id = str(target.get("message_id") or "").strip() or None
        is_group = bool(target.get("is_group"))
        result = await send_message(
            self._channel,
            chat_id=chat_id,
            payload={"markdown": content},
            reply_to=message_id if (is_group and message_id) else None,
            uuid=self._message_uuid(f"scheduled:{task.task_id}"),
            logger=self.logger,
            message_id=message_id,
        )
        if getattr(result, "success", False) is False:
            raise RuntimeError(f"Feishu scheduled send failed: {getattr(result, 'error', None)}")
        message_handler = getattr(self.agent, "message_handler", None)
        store_model_reply = getattr(message_handler, "store_model_reply", None)
        if callable(store_model_reply):
            try:
                await store_model_reply(
                    content,
                    getattr(self.agent, "_assistant_sender_id", "agent"),
                    metadata={
                        "scheduled_task": {
                            "id": task.task_id,
                            "name": task.name,
                            "type": task.task_type,
                            "run_at": task.run_at.isoformat(sep=" "),
                            "delivery": task.delivery,
                        }
                    },
                )
            except Exception:
                self.logger.debug("Failed to persist Feishu scheduled task result", exc_info=True)

    async def _scheduled_task_result(self, task) -> str:
        task_type = task.task_type
        if task_type == "message":
            return task.content.strip()
        if task_type != "agent":
            raise ValueError(f"unsupported scheduled Feishu task type: {task_type}")

        chat = getattr(self.agent, "chat", None)
        if not callable(chat):
            raise RuntimeError("Agent does not support chat().")
        execution = self._scheduled_execution_options(task)
        user_id = task.delivery_user_id or str(task.target.get("sender_id") or AgentConfig.DEFAULT_USER_ID)
        context = ScheduledDeliveryContext(
            channel="feishu",
            user_id=user_id,
            target=task.delivery.get("target") if isinstance(task.delivery.get("target"), dict) else {},
            metadata={
                "source": "scheduled_task",
                "task_id": task.task_id,
                "task_name": task.name,
                "task_type": task.task_type,
            },
        )
        with scheduled_delivery_context(context):
            response = await chat(
                user_message=self._scheduled_agent_prompt(task.content),
                user_id=user_id,
                history_count=execution["history_count"],
                max_iter=execution["max_iter"],
                max_concurrent_tools=execution["max_concurrent_tools"],
                enable_memory=execution["enable_memory"],
            )
        return self._stringify_scheduled_agent_response(response).strip()

    @staticmethod
    def _scheduled_agent_prompt(content: str) -> str:
        return (
            "This scheduled task is now due. Execute it now and return the final message "
            "that should be delivered to the user.\n\n"
            f"Task: {content.strip()}"
        )

    @staticmethod
    def _scheduled_execution_options(task) -> dict[str, Any]:
        execution = task.execution
        return {
            "history_count": FeishuAdapter._positive_int(
                execution.get("history_count"),
                AgentConfig.DEFAULT_HISTORY_COUNT,
            ),
            "max_iter": FeishuAdapter._positive_int(
                execution.get("max_iter"),
                AgentConfig.DEFAULT_MAX_ITER,
            ),
            "max_concurrent_tools": FeishuAdapter._positive_int(
                execution.get("max_concurrent_tools"),
                AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS,
            ),
            "enable_memory": bool(execution.get("enable_memory", True)),
        }

    @staticmethod
    def _positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return default
        return parsed if parsed > 0 else default

    @staticmethod
    def _stringify_scheduled_agent_response(response: Any) -> str:
        if isinstance(response, str):
            return response
        if hasattr(response, "model_dump"):
            try:
                return json.dumps(response.model_dump(), ensure_ascii=False)
            except Exception:
                return str(response)
        return str(response)

    def _chat_kwargs(
        self,
        *,
        user_id: str,
        text: str,
        image_sources: Optional[list[str]] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user_message": text,
            "user_id": user_id,
            "enable_memory": self.config.enable_memory,
        }
        if image_sources:
            kwargs["image_source"] = image_sources[0] if len(image_sources) == 1 else image_sources
        if attachments:
            kwargs["attachments"] = attachments
        if self.config.history_count is not None:
            kwargs["history_count"] = self.config.history_count
        if self.config.max_iter is not None:
            kwargs["max_iter"] = self.config.max_iter
        if self.config.max_concurrent_tools is not None:
            kwargs["max_concurrent_tools"] = self.config.max_concurrent_tools
        return kwargs

    @staticmethod
    def _image_sources_for_model(image_assets: list[_FeishuInboundImageAsset]) -> list[str]:
        return [asset.image_source for asset in image_assets if asset.image_source]

    def _outbound_attachments_from_inbound_images(
        self,
        image_assets: list[_FeishuInboundImageAsset],
    ) -> list[_FeishuOutboundAttachment]:
        attachments: list[_FeishuOutboundAttachment] = []
        for asset in image_assets:
            source = asset.blob_url or asset.path or asset.image_source
            path = self._resolve_outbound_workspace_path(source)
            if path is None:
                continue
            attachments.append(_FeishuOutboundAttachment(
                kind="image",
                path=path,
                blob_url=asset.blob_url or self._path_to_workspace_blob_url(path),
            ))
        return self._dedupe_outbound_attachments(attachments)

    @staticmethod
    def _image_assets_from_attachment_assets(
        assets: list[_FeishuInboundAttachmentAsset],
    ) -> list[_FeishuInboundImageAsset]:
        image_assets: list[_FeishuInboundImageAsset] = []
        for asset in assets:
            attachment = asset.attachment or {}
            if not asset.image_source and attachment.get("kind") != "image":
                continue
            image_assets.append(_FeishuInboundImageAsset(
                image_source=asset.image_source or str(attachment.get("blob_url") or ""),
                path=str(attachment.get("path") or ""),
                blob_url=str(attachment.get("blob_url") or ""),
                markdown=asset.markdown,
            ))
        return image_assets

    @staticmethod
    def _attachments_from_attachment_assets(
        assets: list[_FeishuInboundAttachmentAsset],
    ) -> list[dict[str, Any]]:
        return [asset.attachment for asset in assets if asset.attachment]

    @staticmethod
    def _append_image_markdown_context(text: str, image_assets: list[_FeishuInboundImageAsset]) -> str:
        text = FeishuAdapter._strip_redundant_feishu_image_markdown(text) if image_assets else text
        markdowns = [asset.markdown for asset in image_assets if asset.markdown]
        if not markdowns:
            return text
        stripped_text = text.strip()
        if not stripped_text:
            return "\n\n".join(markdowns)
        return f"{stripped_text}\n\n" + "\n\n".join(markdowns)

    @staticmethod
    def _strip_redundant_feishu_image_markdown(text: str) -> str:
        def replace(match: re.Match[str]) -> str:
            source = (match.group(1) or "").strip()
            if FeishuAdapter._is_redundant_feishu_image_source(source):
                return ""
            return match.group(0)

        cleaned = _MARKDOWN_IMAGE_RE.sub(replace, text or "")
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

    @staticmethod
    def _is_redundant_feishu_image_source(source: str) -> bool:
        if not source:
            return False
        parsed = urlparse(source)
        if parsed.scheme in {"http", "https", "data", "blob"}:
            return False
        if source.startswith("/api/workspace/blob?") or source.startswith("/"):
            return False
        return source.startswith("img_")

    async def _send_attachment_download_failed(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        raw_msg: Any,
        is_group: bool,
        failures: list[_FeishuInboundImageDownloadFailure],
    ) -> None:
        for failure in failures:
            self.logger.warning(
                "Feishu inbound resource download failed: message_id=%s file_key=%s resource_type=%s reason=%s",
                message_id,
                failure.file_key,
                failure.resource_type,
                failure.reason,
            )
        only_images = bool(failures) and all(failure.resource_type == "image" for failure in failures)
        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        await self._send_markdown(
            chat_id=chat_id,
            message_id=anchor,
            uuid_message_id=self._event_message_uuid(message_id, 1),
            text="图片下载失败，请重试或重新发送。" if only_images else "附件下载失败，请重试或重新发送。",
            is_group=is_group,
        )

    async def _send_image_download_failed(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        raw_msg: Any,
        is_group: bool,
        failures: list[_FeishuInboundImageDownloadFailure],
    ) -> None:
        for failure in failures:
            self.logger.warning(
                "Feishu inbound image download failed: message_id=%s file_key=%s resource_type=%s reason=%s",
                message_id,
                failure.file_key,
                failure.resource_type,
                failure.reason,
            )
        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        await self._send_markdown(
            chat_id=chat_id,
            message_id=anchor,
            uuid_message_id=self._event_message_uuid(message_id, 1),
            text="图片下载失败，请重试或重新发送。",
            is_group=is_group,
        )

    async def _send_event_replies(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        is_group: bool,
        chat_kwargs: dict[str, Any],
        raw_msg: Any = None,
    ) -> None:
        chat_events = getattr(self.agent, "chat_events", None)
        if not callable(chat_events):
            raise RuntimeError("Agent does not support chat_events().")

        if self.config.stream and callable(getattr(self._channel, "stream", None)):
            await self._send_event_streaming_cards(
                chat_id=chat_id,
                message_id=message_id,
                is_group=is_group,
                chat_kwargs=chat_kwargs,
                raw_msg=raw_msg,
            )
            return

        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        sent_count = 0
        async for event in chat_events(**chat_kwargs, stream=False):
            event_type = event.get("type")
            if event_type == "message_done":
                content = str(event.get("content") or "").strip()
                attachments = self._outbound_attachments_from_event(event)
                if not content and not attachments:
                    continue
                sent_count += 1
                uuid_message_id = self._event_message_uuid(message_id, sent_count)
                await self._send_markdown(
                    chat_id=chat_id,
                    message_id=anchor,
                    uuid_message_id=uuid_message_id,
                    text=content,
                    is_group=is_group,
                    attachments=attachments,
                )
            elif event_type == "error":
                sent_count += 1
                uuid_message_id = self._event_message_uuid(message_id, sent_count)
                await self._send_markdown(
                    chat_id=chat_id,
                    message_id=anchor,
                    uuid_message_id=uuid_message_id,
                    text=str(event.get("error") or "Agent processing error."),
                    is_group=is_group,
                )

    async def _send_event_streaming_cards(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        is_group: bool,
        chat_kwargs: dict[str, Any],
        raw_msg: Any = None,
    ) -> None:
        chat_events = getattr(self.agent, "chat_events", None)
        if not callable(chat_events):
            raise RuntimeError("Agent does not support chat_events().")

        anchor = self._reply_anchor(raw_msg=raw_msg, message_id=message_id)
        assert self._channel is not None
        sent_count = 0
        active_queue: Optional[asyncio.Queue[Optional[str]]] = None
        active_task: Optional[asyncio.Task[Any]] = None
        active_has_delta = False

        async def start_card() -> None:
            nonlocal active_queue, active_task, active_has_delta, sent_count
            sent_count += 1
            queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
            uuid_message_id = self._event_message_uuid(message_id, sent_count)

            async def producer(stream):
                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    if chunk:
                        await stream.append(chunk)

            opts = self._send_opts(
                message_id=anchor,
                is_group=is_group,
                uuid_message_id=uuid_message_id,
            )
            active_queue = queue
            active_task = asyncio.create_task(self._channel.stream(chat_id, {"markdown": producer}, opts))
            active_has_delta = False

        async def finish_card(
            final_content: str = "",
            extra_attachments: Optional[list[_FeishuOutboundAttachment]] = None,
        ) -> None:
            nonlocal active_queue, active_task, active_has_delta, sent_count
            explicit_attachments = self._dedupe_outbound_attachments(extra_attachments or [])
            seen_paths = {attachment.path for attachment in explicit_attachments}
            cleaned_content, parsed_attachments = self._split_outbound_attachments(
                final_content,
                seen_paths=seen_paths,
            )
            attachments = [*explicit_attachments, *parsed_attachments]
            if active_queue is None or active_task is None:
                if cleaned_content.strip() or attachments:
                    sent_count += 1
                    await self._send_markdown(
                        chat_id=chat_id,
                        message_id=anchor,
                        uuid_message_id=self._event_message_uuid(message_id, sent_count),
                        text=cleaned_content,
                        is_group=is_group,
                        attachments=attachments,
                    )
                return

            if cleaned_content and not active_has_delta:
                await active_queue.put(cleaned_content)
            await active_queue.put(None)
            result = await active_task
            self._log_send_result(result=result, chat_id=chat_id, message_id=anchor)
            active_queue = None
            active_task = None
            active_has_delta = False
            if attachments:
                await self._send_outbound_attachments(
                    chat_id=chat_id,
                    message_id=anchor,
                    uuid_message_id=self._event_message_uuid(message_id, sent_count),
                    attachments=attachments,
                    is_group=is_group,
                )

        async for event in chat_events(**chat_kwargs, stream=True):
            event_type = event.get("type")
            if event_type == "message_start":
                if active_queue is not None:
                    await finish_card()
                continue
            if event_type == "message_delta":
                if active_queue is None:
                    await start_card()
                delta = str(event.get("delta") or "")
                if delta:
                    await active_queue.put(delta)
                    active_has_delta = True
                continue
            if event_type == "message_done":
                content = str(event.get("content") or "")
                await finish_card(content, self._outbound_attachments_from_event(event))
                continue
            if event_type == "error":
                await finish_card()
                sent_count += 1
                await self._send_markdown(
                    chat_id=chat_id,
                    message_id=anchor,
                    uuid_message_id=self._event_message_uuid(message_id, sent_count),
                    text=str(event.get("error") or "Agent processing error."),
                    is_group=is_group,
                )

        if active_queue is not None:
            await finish_card()

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
    def _event_message_uuid(message_id: Optional[str], count: int) -> Optional[str]:
        if not message_id:
            return None
        return message_id if count <= 1 else f"{message_id}:{count}"

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
        attachments: Optional[list[_FeishuOutboundAttachment]] = None,
    ) -> None:
        assert self._channel is not None
        explicit_attachments = self._dedupe_outbound_attachments(attachments or [])
        seen_paths = {attachment.path for attachment in explicit_attachments}
        text, parsed_attachments = self._split_outbound_attachments(text, seen_paths=seen_paths)
        outbound_attachments = [*explicit_attachments, *parsed_attachments]
        try:
            if text.strip():
                await send_message(
                    self._channel,
                    chat_id=chat_id,
                    payload={"markdown": text},
                    reply_to=message_id if (is_group and message_id) else None,
                    uuid=self._message_uuid(uuid_message_id or message_id),
                    logger=self.logger,
                    message_id=message_id,
                )
            if outbound_attachments:
                await self._send_outbound_attachments(
                    chat_id=chat_id,
                    message_id=message_id,
                    uuid_message_id=uuid_message_id or message_id,
                    attachments=outbound_attachments,
                    is_group=is_group,
                )
        except Exception:
            self.logger.exception("Failed to send Feishu message to %s", chat_id)

    async def _send_outbound_attachments(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid_message_id: Optional[str],
        attachments: list[_FeishuOutboundAttachment],
        is_group: bool,
    ) -> None:
        await self._artifact_renderer.send(
            chat_id=chat_id,
            message_id=message_id,
            uuid_message_id=uuid_message_id,
            attachments=attachments,
            is_group=is_group,
        )

    async def _send_outbound_attachments_separate(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid_message_id: Optional[str],
        attachments: list[_FeishuOutboundAttachment],
        is_group: bool,
    ) -> None:
        assert self._channel is not None
        for index, attachment in enumerate(self._dedupe_outbound_attachments(attachments), start=1):
            await self._send_attachment_with_fallback(
                chat_id=chat_id,
                message_id=message_id,
                uuid_message_id=uuid_message_id,
                attachment=attachment,
                is_group=is_group,
                index=index,
            )

    async def _send_attachment_with_fallback(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid_message_id: Optional[str],
        attachment: _FeishuOutboundAttachment,
        is_group: bool,
        index: int,
    ) -> None:
        base_uuid = uuid_message_id or message_id
        media_uuid = f"{base_uuid}:media:{index}" if base_uuid else None
        if attachment.caption.strip():
            await self._send_attachment_caption(
                chat_id=chat_id,
                message_id=message_id,
                uuid=self._message_uuid(f"{media_uuid}:caption" if media_uuid else None),
                caption=attachment.caption.strip(),
                is_group=is_group,
            )

        send_path = attachment.path
        if attachment.kind == "image":
            send_path = self._prepare_feishu_outbound_image_path(attachment.path)
            image_payload = {"image": {"source": str(send_path)}}
            image_result = await self._send_payload_with_retries(
                chat_id=chat_id,
                message_id=message_id,
                uuid=self._message_uuid(media_uuid),
                payload=image_payload,
                is_group=is_group,
                attempts=2,
            )
            if self._send_result_success(image_result):
                return
            self.logger.warning(
                "Feishu image attachment send failed after retry; falling back to file: chat_id=%s path=%s",
                chat_id,
                send_path,
            )

        file_payload = {"file": {"source": str(send_path), "file_name": send_path.name}}
        file_result = await self._send_payload_with_retries(
            chat_id=chat_id,
            message_id=message_id,
            uuid=self._message_uuid(f"{media_uuid}:file" if media_uuid else None),
            payload=file_payload,
            is_group=is_group,
            attempts=2,
        )
        if self._send_result_success(file_result):
            return
        await self._send_attachment_failure_notice(
            chat_id=chat_id,
            message_id=message_id,
            uuid=self._message_uuid(f"{media_uuid}:error" if media_uuid else None),
            attachment=attachment,
            is_group=is_group,
        )

    def _prepare_feishu_outbound_image_path(self, path: Path) -> Path:
        source_path = Path(path).expanduser().resolve()
        if not source_path.is_file():
            return source_path
        try:
            image_bytes = source_path.read_bytes()
        except OSError:
            return source_path
        detected_mime_type = self._detect_image_mime(image_bytes)
        guessed_mime_type, _ = mimetypes.guess_type(source_path.name)
        mime_type = detected_mime_type or guessed_mime_type or "image/jpeg"
        image = _FeishuImageResource(data=image_bytes, mime_type=mime_type, file_name=source_path.name)
        compressed_image = self._compress_feishu_image_resource(
            image,
            direction="outbound",
            path=source_path,
        )
        if compressed_image.data == image_bytes:
            return source_path

        workspace_root = self._agent_workspace_root()
        if workspace_root is None:
            return source_path
        output_dir = (workspace_root / _FEISHU_OUTBOUND_IMAGE_OUTPUT_DIR).resolve()
        if not output_dir.is_relative_to(workspace_root):
            return source_path
        output_dir.mkdir(parents=True, exist_ok=True)

        source_hash = hashlib.sha256(image_bytes).hexdigest()[:12]
        settings_hash = hashlib.sha1(
            f"{_FEISHU_IMAGE_TRANSPORT_MAX_BYTES}:{_FEISHU_IMAGE_TRANSPORT_MAX_EDGE}".encode("utf-8")
        ).hexdigest()[:8]
        stem = self._safe_filename_part(Path(compressed_image.file_name).stem or source_path.stem or "image")
        extension = self._image_extension(compressed_image.mime_type, compressed_image.file_name)
        target_path = (output_dir / f"{stem}-{source_hash}-{settings_hash}.{extension}").resolve()
        if not target_path.is_relative_to(workspace_root):
            return source_path
        if not target_path.exists() or target_path.read_bytes() != compressed_image.data:
            target_path.write_bytes(compressed_image.data)
        return target_path

    async def _send_attachment_caption(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid: Optional[str],
        caption: str,
        is_group: bool,
    ) -> None:
        try:
            await send_message(
                self._channel,
                chat_id=chat_id,
                payload={"markdown": caption},
                reply_to=message_id if (is_group and message_id) else None,
                uuid=uuid,
                logger=self.logger,
                message_id=message_id,
            )
        except Exception:
            self.logger.exception("Failed to send Feishu attachment caption to %s", chat_id)

    async def _send_payload_with_retries(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid: Optional[str],
        payload: dict[str, Any],
        is_group: bool,
        attempts: int,
    ) -> Any:
        result: Any = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                result = await send_message(
                    self._channel,
                    chat_id=chat_id,
                    payload=payload,
                    reply_to=message_id if (is_group and message_id) else None,
                    uuid=uuid,
                    logger=self.logger,
                    message_id=message_id,
                )
                if self._send_result_success(result):
                    return result
            except Exception as exc:
                result = exc
                self.logger.warning(
                    "Feishu attachment send attempt failed: chat_id=%s message_id=%s attempt=%s error=%s",
                    chat_id,
                    message_id,
                    attempt,
                    exc,
                )
            if attempt < attempts:
                await asyncio.sleep(0.5 * attempt)
        return result

    async def _send_attachment_failure_notice(
        self,
        *,
        chat_id: str,
        message_id: Optional[str],
        uuid: Optional[str],
        attachment: _FeishuOutboundAttachment,
        is_group: bool,
    ) -> None:
        label = "图片" if attachment.kind == "image" else "文件"
        blob_url = attachment.blob_url or self._path_to_workspace_blob_url(attachment.path)
        link_text = f"\n{blob_url}" if blob_url else ""
        try:
            await send_message(
                self._channel,
                chat_id=chat_id,
                payload={"markdown": f"{label}发送失败，已保存到 workspace：{link_text}"},
                reply_to=message_id if (is_group and message_id) else None,
                uuid=uuid,
                logger=self.logger,
                message_id=message_id,
            )
        except Exception:
            self.logger.exception("Failed to send Feishu attachment failure notice to %s", chat_id)

    @staticmethod
    def _send_result_success(result: Any) -> bool:
        return not isinstance(result, Exception) and bool(getattr(result, "success", True))

    def _split_outbound_attachments(
        self,
        text: str,
        *,
        seen_paths: Optional[set[Path]] = None,
    ) -> tuple[str, list[_FeishuOutboundAttachment]]:
        if not isinstance(text, str) or not text:
            return "", []
        attachments: list[_FeishuOutboundAttachment] = []
        spans: list[tuple[int, int]] = []
        seen_paths = set(seen_paths or set())

        def add_attachment(match: re.Match[str]) -> None:
            source = match.group(1).strip()
            path = self._resolve_outbound_workspace_path(source)
            if path is None:
                return
            spans.append(match.span())
            if path in seen_paths:
                return
            kind = "image" if self._is_outbound_image(path) else "file"
            attachments.append(_FeishuOutboundAttachment(
                kind=kind,
                path=path,
                blob_url=self._path_to_workspace_blob_url(path),
            ))
            seen_paths.add(path)

        for match in _MARKDOWN_IMAGE_RE.finditer(text):
            add_attachment(match)
        for match in _MARKDOWN_LINK_RE.finditer(text):
            add_attachment(match)

        if not spans:
            return text, []
        return self._remove_spans(text, spans), attachments

    def _outbound_attachments_from_event(self, event: dict[str, Any]) -> list[_FeishuOutboundAttachment]:
        raw_attachments = event.get("attachments")
        if not isinstance(raw_attachments, list):
            return []
        attachments: list[_FeishuOutboundAttachment] = []
        seen_paths: set[Path] = set()
        for item in raw_attachments:
            if not isinstance(item, dict):
                continue
            source = str(item.get("blob_url") or item.get("path") or "").strip()
            path = self._resolve_outbound_workspace_path(source)
            if path is None or path in seen_paths:
                continue
            raw_kind = str(item.get("kind") or "").strip().lower()
            kind = raw_kind if raw_kind in {"image", "file"} else ("image" if self._is_outbound_image(path) else "file")
            blob_url = str(item.get("blob_url") or "").strip() or self._path_to_workspace_blob_url(path)
            attachments.append(_FeishuOutboundAttachment(
                kind=kind,
                path=path,
                caption=str(item.get("caption") or "").strip(),
                blob_url=blob_url,
            ))
            seen_paths.add(path)
        return attachments

    @staticmethod
    def _dedupe_outbound_attachments(
        attachments: list[_FeishuOutboundAttachment],
    ) -> list[_FeishuOutboundAttachment]:
        deduped: list[_FeishuOutboundAttachment] = []
        seen_paths: set[Path] = set()
        for attachment in attachments:
            path = attachment.path.expanduser().resolve()
            if path in seen_paths or not path.is_file():
                continue
            seen_paths.add(path)
            deduped.append(_FeishuOutboundAttachment(
                kind=attachment.kind,
                path=path,
                caption=attachment.caption,
                blob_url=attachment.blob_url,
            ))
        return deduped

    def _resolve_outbound_workspace_path(self, source: str) -> Optional[Path]:
        workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            return None
        workspace_root = Path(workspace_dir).expanduser().resolve()
        source = source.strip().strip("<>")
        if not source:
            return None

        relative_path = self._workspace_blob_relative_path(source)
        if relative_path:
            candidate = (workspace_root / relative_path).resolve()
        else:
            parsed = urlparse(source)
            if parsed.scheme and parsed.scheme != "file":
                return None
            raw_path = unquote(parsed.path if parsed.scheme == "file" else source)
            candidate_path = Path(raw_path).expanduser()
            candidate = candidate_path.resolve() if candidate_path.is_absolute() else (workspace_root / raw_path).resolve()

        if not candidate.is_relative_to(workspace_root) or not candidate.is_file():
            return None
        return candidate

    def _path_to_workspace_blob_url(self, path: Path) -> str:
        workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            return ""
        workspace_root = Path(workspace_dir).expanduser().resolve()
        resolved_path = path.expanduser().resolve()
        if not resolved_path.is_relative_to(workspace_root):
            return ""
        return workspace_blob_url(resolved_path.relative_to(workspace_root).as_posix())

    @staticmethod
    def _workspace_blob_relative_path(source: str) -> str:
        parsed = urlparse(source)
        if parsed.path != "/api/workspace/blob":
            return ""
        values = parse_qs(parsed.query).get("path") or []
        return unquote(values[0]).strip("/") if values else ""

    @staticmethod
    def _is_outbound_image(path: Path) -> bool:
        mime_type, _ = mimetypes.guess_type(path.name)
        return bool(mime_type and mime_type.startswith("image/"))

    @staticmethod
    def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
        pieces: list[str] = []
        last_index = 0
        for start, end in sorted(spans):
            if start < last_index:
                continue
            pieces.append(text[last_index:start])
            last_index = end
        pieces.append(text[last_index:])
        cleaned = "".join(pieces)
        cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
        return cleaned.strip()

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
