"""Weixin iLink DM <-> xAgent bridge."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from ...core.agent import Agent
from ...core.config import AgentConfig
from ...core.runtime import AsyncTaskScheduler, ScheduledDeliveryContext, scheduled_delivery_context
from ...schemas.attachment import (
    ATTACHMENT_KIND_IMAGE,
    attachment_kind,
    attachment_markdown,
    save_workspace_attachment_bytes,
    workspace_attachment_from_path,
)
from ...utils.image_utils import (
    DEFAULT_IMAGE_TRANSPORT_MAX_BYTES,
    DEFAULT_IMAGE_TRANSPORT_MAX_EDGE,
    compress_image_bytes_for_transport,
    workspace_blob_url,
)
from .client import SESSION_EXPIRED_ERRCODE, WeixinClient, raise_for_api_error
from .config import WeixinAdapterConfig
from .media import ITEM_FILE, ITEM_IMAGE, ITEM_VIDEO, ITEM_VOICE, InboundMedia, download_inbound_media, upload_outbound_media
from .send import extract_text, make_client_id, split_text
from .state import WeixinCredentials, WeixinStateStore


WEIXIN_INBOUND_IMAGE_DIR = "temp/images/weixin"
WEIXIN_OUTBOUND_IMAGE_DIR = "temp/images/weixin/outbound"
WEIXIN_ATTACHMENT_DIR = "temp/attachments/weixin"
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class _WeixinOutboundAttachment:
    kind: str
    path: Path
    caption: str = ""
    blob_url: str = ""


@dataclass(frozen=True)
class _WeixinScheduledTaskResult:
    content: str
    attachments: list[_WeixinOutboundAttachment]


@dataclass(frozen=True)
class _WeixinInboundPayload:
    text: str
    image_sources: list[str]
    attachments: list[dict[str, Any]]


class _TypingTicketCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        ticket, created_at = entry
        if time.time() - created_at >= self.ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return ticket

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


class _MessageDeduplicator:
    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._seen: dict[str, float] = {}

    def is_duplicate(self, key: str) -> bool:
        now = time.time()
        cutoff = now - self.ttl_seconds
        for existing, timestamp in list(self._seen.items()):
            if timestamp < cutoff:
                self._seen.pop(existing, None)
        if key in self._seen:
            return True
        self._seen[key] = now
        return False


class WeixinAdapter:
    """Bridge iLink direct messages to an in-process xAgent instance."""

    def __init__(
        self,
        agent: Agent,
        config: WeixinAdapterConfig,
        *,
        runtime_dir: str | Path,
        logger: Optional[logging.Logger] = None,
        client: Optional[WeixinClient] = None,
        state_store: Optional[WeixinStateStore] = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.logger = logger or logging.getLogger(self.__class__.__name__)
        self.state_store = state_store or WeixinStateStore(self.runtime_dir)
        self.client = client
        self._owns_client = client is None
        self._credentials: Optional[WeixinCredentials] = None
        self._context_tokens: dict[str, str] = {}
        self._chat_locks: dict[str, asyncio.Lock] = {}
        self._processing_tasks: set[asyncio.Task[None]] = set()
        self._processing_tasks_lock = asyncio.Lock()
        self._typing_cache = _TypingTicketCache(config.typing_ticket_ttl_seconds)
        self._dedup = _MessageDeduplicator()
        self._stop_event = asyncio.Event()
        self._tasks_dir = self.runtime_dir / AgentConfig.TASKS_DIRNAME
        self._task_scheduler: Optional[AsyncTaskScheduler] = None

    async def run(self) -> None:
        credentials = self.state_store.load_credentials(self.config.account_id)
        if credentials is None:
            raise RuntimeError("Weixin channel credentials are missing. Run: xagent channel weixin setup")
        self._credentials = credentials
        self._context_tokens = self.state_store.load_context_tokens(credentials.account_id)
        if self.client is None:
            self.client = WeixinClient(
                base_url=credentials.base_url or self.config.base_url,
                token=credentials.token,
                channel_version=self.config.channel_version,
                cdn_base_url=self.config.cdn_base_url,
            )
        else:
            self.client.with_credentials(credentials)

        task_scheduler = AsyncTaskScheduler(
            self._tasks_dir,
            can_handle=self._can_handle_scheduled_task,
            dispatch=self._dispatch_scheduled_task,
            logger_=self.logger,
        )
        self._task_scheduler = task_scheduler
        await task_scheduler.start()
        try:
            await self._poll_loop()
        finally:
            await task_scheduler.stop()
            self._task_scheduler = None
            await self._cancel_processing_tasks()
            if self._owns_client and self.client is not None:
                await self.client.aclose()
            flusher = getattr(self.agent, "run_memory_maintenance", None)
            if callable(flusher):
                await flusher()

    async def stop(self) -> None:
        self._stop_event.set()

    async def _poll_loop(self) -> None:
        assert self.client is not None and self._credentials is not None
        sync_buf = self.state_store.load_sync_buf(self._credentials.account_id)
        timeout_ms = self.config.poll_timeout_ms
        consecutive_failures = 0
        self.logger.info("Weixin long-poll started account=%s", _safe_id(self._credentials.account_id))

        while not self._stop_event.is_set():
            try:
                response = await self.client.get_updates(sync_buf=sync_buf, timeout_ms=timeout_ms)
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if _is_error(ret, errcode):
                    if _error_code(ret, errcode) == SESSION_EXPIRED_ERRCODE:
                        self.logger.error("Weixin session expired. Stop the channel and rerun: xagent channel weixin setup --force")
                        self.state_store.clear_sync_buf(self._credentials.account_id)
                        self.state_store.clear_context_tokens(self._credentials.account_id)
                        self.state_store.delete_credentials(self._credentials.account_id)
                        self._context_tokens.clear()
                        self._stop_event.set()
                        raise RuntimeError("Weixin session expired. Run setup again.")
                    consecutive_failures += 1
                    self.logger.warning(
                        "Weixin getupdates failed ret=%s errcode=%s errmsg=%s (%s/%s)",
                        ret,
                        errcode,
                        response.get("errmsg", ""),
                        consecutive_failures,
                        self.config.max_consecutive_failures,
                    )
                    await self._poll_backoff(consecutive_failures)
                    if consecutive_failures >= self.config.max_consecutive_failures:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    self.state_store.save_sync_buf(self._credentials.account_id, sync_buf)

                for message in response.get("msgs") or []:
                    if isinstance(message, dict):
                        await self._create_processing_task(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop_event.is_set():
                    raise
                consecutive_failures += 1
                self.logger.exception(
                    "Weixin poll error (%s/%s): %s",
                    consecutive_failures,
                    self.config.max_consecutive_failures,
                    exc,
                )
                await self._poll_backoff(consecutive_failures)
                if consecutive_failures >= self.config.max_consecutive_failures:
                    consecutive_failures = 0

    async def _poll_backoff(self, consecutive_failures: int) -> None:
        delay = self.config.backoff_delay_seconds if consecutive_failures >= self.config.max_consecutive_failures else self.config.retry_delay_seconds
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _create_processing_task(self, message: dict[str, Any]) -> None:
        task = asyncio.create_task(self._process_message_safe(message))
        async with self._processing_tasks_lock:
            self._processing_tasks.add(task)
        task.add_done_callback(lambda item: asyncio.create_task(self._discard_processing_task(item)))

    async def _discard_processing_task(self, task: asyncio.Task[None]) -> None:
        async with self._processing_tasks_lock:
            self._processing_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.exception("Unhandled Weixin message processing error")

    async def _cancel_processing_tasks(self) -> None:
        async with self._processing_tasks_lock:
            tasks = list(self._processing_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_message_safe(self, message: dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except Exception:
            self.logger.exception("Weixin inbound processing failed from=%s", _safe_id(message.get("from_user_id")))

    async def _process_message(self, message: dict[str, Any]) -> None:
        if not self._should_route_message(message):
            return
        user_id = str(message.get("from_user_id") or "").strip()
        lock = self._chat_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._chat_locks[user_id] = lock
        async with lock:
            await self._handle_dm(message, user_id=user_id)

    def _should_route_message(self, message: dict[str, Any]) -> bool:
        if int(message.get("message_type") or 0) != 1:
            return False
        user_id = str(message.get("from_user_id") or "").strip()
        if not user_id or user_id == self.config.account_id:
            return False
        if self._credentials and user_id == self._credentials.account_id:
            return False
        if self._looks_like_group_message(message):
            return False
        message_id = str(message.get("message_id") or message.get("client_id") or "").strip()
        if message_id and self._dedup.is_duplicate(f"message:{message_id}"):
            return False
        if not str(message.get("context_token") or "").strip():
            self.logger.debug("Skipping Weixin message without context_token from=%s", _safe_id(user_id))
            return False
        return self._is_user_allowed(user_id)

    def _is_user_allowed(self, user_id: str) -> bool:
        allow_users = set(self.config.allow_users)
        owner_user_id = self.config.owner_user_id or (self._credentials.user_id if self._credentials else "")
        if self.config.owner_only:
            return user_id == owner_user_id or user_id in allow_users
        if allow_users:
            return user_id in allow_users or user_id == owner_user_id
        return True

    @staticmethod
    def _looks_like_group_message(message: dict[str, Any]) -> bool:
        if message.get("group_id"):
            return True
        values = [message.get("from_user_id"), message.get("to_user_id"), message.get("session_id")]
        return any("@chatroom" in str(value or "") for value in values)

    async def _handle_dm(self, message: dict[str, Any], *, user_id: str) -> None:
        assert self._credentials is not None
        context_token = str(message.get("context_token") or "").strip()
        self._remember_context(user_id, context_token)
        inbound = await self._build_inbound_payload(message)
        text = inbound.text.strip()
        if not text and inbound.attachments:
            text = "The user sent attachments."
        if not text:
            return

        chat_kwargs = self._chat_kwargs(user_id=user_id, text=text, inbound=inbound)
        context = ScheduledDeliveryContext(
            channel="weixin",
            user_id=user_id,
            target={
                "user_id": user_id,
                "account_id": self._credentials.account_id,
            },
            metadata={
                "source": "weixin",
                "message_id": str(message.get("message_id") or ""),
            },
        )
        typing_task: Optional[asyncio.Task[None]] = None
        try:
            if self.config.send_typing:
                typing_task = asyncio.create_task(self._typing_keepalive(user_id, context_token))
            with scheduled_delivery_context(context):
                await self._send_event_replies(user_id=user_id, context_token=context_token, source_message_id=str(message.get("message_id") or ""), chat_kwargs=chat_kwargs)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                await asyncio.gather(typing_task, return_exceptions=True)
            if self.config.send_typing:
                await self._stop_typing(user_id, context_token)

    def _remember_context(self, user_id: str, context_token: str) -> None:
        assert self._credentials is not None
        self._context_tokens[user_id] = context_token
        self.state_store.save_context_tokens(self._credentials.account_id, self._context_tokens)
        self.state_store.save_last_active_user(
            account_id=self._credentials.account_id,
            user_id=user_id,
            context_token=context_token,
        )

    async def _build_inbound_payload(self, message: dict[str, Any]) -> _WeixinInboundPayload:
        item_list = [item for item in message.get("item_list") or [] if isinstance(item, dict)]
        text = extract_text(item_list).strip()
        attachments: list[dict[str, Any]] = []
        image_sources: list[str] = []
        media_notes: list[str] = []
        if self.config.media_enabled:
            for item in item_list:
                inbound_media: Optional[InboundMedia] = None
                try:
                    inbound_media = await download_inbound_media(self.client, item) if self.client is not None else None
                except Exception as exc:
                    self.logger.warning("Weixin media download failed: %s", exc)
                    media_notes.append(_media_failure_note(item))
                    continue
                if inbound_media is None:
                    continue
                try:
                    attachment, image_source, markdown = self._save_inbound_media(inbound_media, str(message.get("message_id") or ""))
                except Exception as exc:
                    self.logger.warning("Weixin media save failed: %s", exc)
                    media_notes.append(_media_failure_note(item))
                    continue
                if attachment:
                    attachments.append(attachment)
                if image_source:
                    image_sources.append(image_source)
                if markdown:
                    media_notes.append(markdown)
        if media_notes:
            text = "\n\n".join(part for part in [text, *media_notes] if part)
        return _WeixinInboundPayload(text=text, image_sources=image_sources, attachments=attachments)

    def _save_inbound_media(self, media: InboundMedia, message_id: str) -> tuple[dict[str, Any], str, str]:
        workspace_root = self._agent_workspace_root()
        if workspace_root is None:
            return {}, "", ""
        source_id = hashlib.sha1(media.resource_id.encode("utf-8")).hexdigest()[:12]
        if attachment_kind(media.mime_type, media.file_name) == ATTACHMENT_KIND_IMAGE:
            data = media.data
            compressed = compress_image_bytes_for_transport(
                data,
                mime_type=media.mime_type,
                file_name=media.file_name,
                max_bytes=DEFAULT_IMAGE_TRANSPORT_MAX_BYTES,
                max_edge=DEFAULT_IMAGE_TRANSPORT_MAX_EDGE,
            )
            output_dir = (workspace_root / WEIXIN_INBOUND_IMAGE_DIR).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            if not output_dir.is_relative_to(workspace_root):
                return {}, "", ""
            content_hash = hashlib.sha256(compressed.data).hexdigest()[:12]
            stem = _safe_filename_part(Path(compressed.file_name).stem or media.resource_type)
            extension = _image_extension(compressed.mime_type, compressed.file_name)
            output_path = (output_dir / f"{stem}-{source_id}-{content_hash}.{extension}").resolve()
            if not output_path.is_relative_to(workspace_root):
                return {}, "", ""
            if not output_path.exists():
                output_path.write_bytes(compressed.data)
            attachment = workspace_attachment_from_path(
                output_path,
                workspace_root,
                caption="Weixin image",
                source_channel="weixin",
                source_message_id=message_id,
                source_resource_id=media.resource_id,
                source_resource_type=media.resource_type,
            )
            image_source = str(attachment.get("blob_url") or "")
            return attachment, image_source, attachment_markdown(attachment)

        attachment = save_workspace_attachment_bytes(
            media.data,
            workspace_root,
            directory=WEIXIN_ATTACHMENT_DIR,
            file_name=media.file_name,
            mime_type=media.mime_type,
            source_channel="weixin",
            source_message_id=message_id,
            source_resource_id=media.resource_id,
            source_resource_type=media.resource_type,
        )
        return attachment, "", attachment_markdown(attachment)

    def _chat_kwargs(self, *, user_id: str, text: str, inbound: _WeixinInboundPayload) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "user_message": text,
            "user_id": user_id,
        }
        if inbound.image_sources and bool(getattr(self.agent, "supports_vision", True)):
            kwargs["image_source"] = inbound.image_sources[0] if len(inbound.image_sources) == 1 else inbound.image_sources
        if inbound.attachments:
            kwargs["attachments"] = inbound.attachments
        if self.config.history_count is not None:
            kwargs["history_count"] = self.config.history_count
        if self.config.max_iter is not None:
            kwargs["max_iter"] = self.config.max_iter
        if self.config.max_concurrent_tools is not None:
            kwargs["max_concurrent_tools"] = self.config.max_concurrent_tools
        return kwargs

    async def _send_event_replies(self, *, user_id: str, context_token: str, source_message_id: str, chat_kwargs: dict[str, Any]) -> None:
        chat_events = getattr(self.agent, "chat_events", None)
        if not callable(chat_events):
            raise RuntimeError("Agent does not support chat_events().")
        sent_count = 0
        async for event in chat_events(**chat_kwargs, stream=False):
            event_type = event.get("type")
            if event_type == "message_done":
                content = str(event.get("content") or "").strip()
                attachments = self._outbound_attachments_from_event(event)
                if not content and not attachments:
                    continue
                sent_count += 1
                await self._send_text_and_attachments(
                    user_id=user_id,
                    context_token=context_token,
                    content=content,
                    attachments=attachments,
                    stable_key=f"{source_message_id or 'message'}:{sent_count}",
                )
            elif event_type == "error":
                sent_count += 1
                await self._send_text(
                    user_id=user_id,
                    context_token=context_token,
                    text=str(event.get("error") or "Agent processing error."),
                    stable_key=f"{source_message_id or 'message'}:error:{sent_count}",
                )

    async def _send_text_and_attachments(
        self,
        *,
        user_id: str,
        context_token: str,
        content: str,
        attachments: list[_WeixinOutboundAttachment],
        stable_key: str,
    ) -> None:
        explicit_attachments = self._dedupe_outbound_attachments(attachments)
        seen_paths = {attachment.path for attachment in explicit_attachments}
        text, parsed = self._split_outbound_attachments(content, seen_paths=seen_paths)
        if text.strip():
            await self._send_text(user_id=user_id, context_token=context_token, text=text, stable_key=stable_key)
        for index, attachment in enumerate([*explicit_attachments, *parsed], start=1):
            await self._send_attachment(user_id=user_id, context_token=context_token, attachment=attachment, stable_key=f"{stable_key}:media:{index}")

    async def _send_text(self, *, user_id: str, context_token: str, text: str, stable_key: str) -> None:
        assert self.client is not None
        chunks = split_text(self._format_message(text), self.config.text_max_chars)
        for index, chunk in enumerate(chunks, start=1):
            client_id = make_client_id(stable_key=f"{stable_key}:text:{index}")
            await self._call_send_with_retries(
                lambda cid=client_id, body=chunk: self.client.send_text_message(
                    to_user_id=user_id,
                    text=body,
                    context_token=context_token,
                    client_id=cid,
                    timeout_ms=self.config.api_timeout_ms,
                ),
                label=f"text:{index}",
            )
            if index < len(chunks) and self.config.send_chunk_delay_seconds > 0:
                await asyncio.sleep(self.config.send_chunk_delay_seconds)

    async def _send_attachment(self, *, user_id: str, context_token: str, attachment: _WeixinOutboundAttachment, stable_key: str) -> None:
        assert self.client is not None
        if attachment.caption.strip():
            await self._send_text(user_id=user_id, context_token=context_token, text=attachment.caption.strip(), stable_key=f"{stable_key}:caption")
        try:
            outbound = await upload_outbound_media(
                self.client,
                to_user_id=user_id,
                path=attachment.path,
                force_file_attachment=attachment.kind != "image" and attachment.path.suffix.lower() in {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac", ".aac"},
            )
            client_id = make_client_id(stable_key=f"{stable_key}:{outbound.client_id_suffix}")
            await self._call_send_with_retries(
                lambda: self.client.send_message_item(
                    to_user_id=user_id,
                    item=outbound.item,
                    context_token=context_token,
                    client_id=client_id,
                    timeout_ms=self.config.api_timeout_ms,
                ),
                label=f"attachment:{attachment.path.name}",
            )
        except Exception as exc:
            self.logger.warning("Weixin attachment send failed path=%s: %s", attachment.path, exc)
            await self._send_text(
                user_id=user_id,
                context_token=context_token,
                text=f"Attachment send failed, saved in workspace: {attachment.blob_url or self._path_to_workspace_blob_url(attachment.path)}",
                stable_key=f"{stable_key}:error",
            )

    async def _call_send_with_retries(self, call, *, label: str) -> dict[str, Any]:
        last_error: Optional[Exception] = None
        attempts = max(1, self.config.send_retries + 1)
        for attempt in range(1, attempts + 1):
            try:
                response = await call()
                raise_for_api_error(response, label)
                return response
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break
                await asyncio.sleep(self.config.send_retry_delay_seconds * attempt)
        assert last_error is not None
        raise last_error

    async def _typing_keepalive(self, user_id: str, context_token: str) -> None:
        try:
            while True:
                await self._send_typing(user_id, context_token, status=1)
                await asyncio.sleep(self.config.typing_keepalive_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.debug("Weixin typing keepalive failed for %s: %s", _safe_id(user_id), exc)

    async def _stop_typing(self, user_id: str, context_token: str) -> None:
        try:
            await self._send_typing(user_id, context_token, status=2)
        except Exception as exc:
            self.logger.debug("Weixin typing stop failed for %s: %s", _safe_id(user_id), exc)

    async def _send_typing(self, user_id: str, context_token: str, *, status: int) -> None:
        if self.client is None:
            return
        ticket = self._typing_cache.get(user_id)
        if not ticket:
            config = await self.client.get_config(user_id=user_id, context_token=context_token, timeout_ms=self.config.api_timeout_ms)
            ticket = str(config.get("typing_ticket") or "")
            if not ticket:
                return
            self._typing_cache.set(user_id, ticket)
        await self.client.send_typing(user_id=user_id, typing_ticket=ticket, status=status, timeout_ms=self.config.api_timeout_ms)

    def _can_handle_scheduled_task(self, task) -> bool:
        return task.kind == "task" and task.delivery_channel == "weixin" and self.client is not None

    async def _dispatch_scheduled_task(self, task) -> None:
        user_id = str(task.target.get("user_id") or task.delivery_user_id or "").strip()
        if not user_id:
            raise ValueError("scheduled Weixin task is missing user_id")
        context_token = self._context_tokens.get(user_id)
        if not context_token:
            raise ValueError(f"scheduled Weixin task cannot send to {user_id}: no cached context_token")
        result = await self._scheduled_task_result(task, user_id=user_id)
        if not result.content and not result.attachments:
            raise ValueError("scheduled Weixin task produced no content")
        await self._send_text_and_attachments(
            user_id=user_id,
            context_token=context_token,
            content=result.content,
            attachments=result.attachments,
            stable_key=f"scheduled:{task.task_id}",
        )

    async def _scheduled_task_result(self, task, *, user_id: str) -> _WeixinScheduledTaskResult:
        if task.task_type == "message":
            return _WeixinScheduledTaskResult(task.content.strip(), [])
        if task.task_type != "agent":
            raise ValueError(f"unsupported scheduled Weixin task type: {task.task_type}")
        chat_events = getattr(self.agent, "chat_events", None)
        if not callable(chat_events):
            raise RuntimeError("Agent does not support chat_events().")
        execution = task.execution
        prompt = (
            "This scheduled task is now due. Execute it now and return the final message "
            "that should be delivered to the user.\n\n"
            f"Task: {task.content.strip()}"
        )
        context = ScheduledDeliveryContext(
            channel="weixin",
            user_id=user_id,
            target=task.target,
            metadata={"source": "scheduled_task", "task_id": task.task_id},
        )
        content = ""
        attachments: list[_WeixinOutboundAttachment] = []
        with scheduled_delivery_context(context):
            async for event in chat_events(
                user_message=prompt,
                user_id=user_id,
                history_count=_positive_int(execution.get("history_count"), AgentConfig.DEFAULT_HISTORY_COUNT),
                max_iter=_positive_int(execution.get("max_iter"), AgentConfig.DEFAULT_MAX_ITER),
                max_concurrent_tools=_positive_int(execution.get("max_concurrent_tools"), AgentConfig.DEFAULT_MAX_CONCURRENT_TOOLS),
                stream=False,
            ):
                if event.get("type") == "message_done" and str(event.get("phase") or "final") == "final":
                    content = str(event.get("content") or "").strip()
                    attachments = self._outbound_attachments_from_event(event)
                elif event.get("type") == "error" and not content:
                    content = str(event.get("error") or "").strip()
        return _WeixinScheduledTaskResult(content, attachments)

    def _split_outbound_attachments(self, text: str, *, seen_paths: Optional[set[Path]] = None) -> tuple[str, list[_WeixinOutboundAttachment]]:
        if not isinstance(text, str) or not text:
            return "", []
        seen_paths = set(seen_paths or set())
        attachments: list[_WeixinOutboundAttachment] = []
        spans: list[tuple[int, int]] = []

        def add(match: re.Match[str]) -> None:
            source = match.group(1).strip()
            path = self._resolve_outbound_workspace_path(source)
            if path is None:
                return
            spans.append(match.span())
            if path in seen_paths:
                return
            attachments.append(_WeixinOutboundAttachment(
                kind="image" if self._is_outbound_image(path) else "file",
                path=path,
                blob_url=self._path_to_workspace_blob_url(path),
            ))
            seen_paths.add(path)

        for match in _MARKDOWN_IMAGE_RE.finditer(text):
            add(match)
        for match in _MARKDOWN_LINK_RE.finditer(text):
            add(match)
        if not spans:
            return text, []
        return _remove_spans(text, spans), attachments

    def _outbound_attachments_from_event(self, event: dict[str, Any]) -> list[_WeixinOutboundAttachment]:
        raw = event.get("attachments")
        if not isinstance(raw, list):
            return []
        result: list[_WeixinOutboundAttachment] = []
        seen: set[Path] = set()
        for item in raw:
            if not isinstance(item, dict):
                continue
            source = str(item.get("blob_url") or item.get("path") or "").strip()
            path = self._resolve_outbound_workspace_path(source)
            if path is None or path in seen:
                continue
            raw_kind = str(item.get("kind") or "").strip().lower()
            result.append(_WeixinOutboundAttachment(
                kind=raw_kind if raw_kind in {"image", "file"} else ("image" if self._is_outbound_image(path) else "file"),
                path=path,
                caption=str(item.get("caption") or "").strip(),
                blob_url=str(item.get("blob_url") or "").strip() or self._path_to_workspace_blob_url(path),
            ))
            seen.add(path)
        return result

    @staticmethod
    def _dedupe_outbound_attachments(attachments: list[_WeixinOutboundAttachment]) -> list[_WeixinOutboundAttachment]:
        deduped: list[_WeixinOutboundAttachment] = []
        seen: set[Path] = set()
        for attachment in attachments:
            path = attachment.path.expanduser().resolve()
            if path in seen or not path.is_file():
                continue
            seen.add(path)
            deduped.append(_WeixinOutboundAttachment(attachment.kind, path, attachment.caption, attachment.blob_url))
        return deduped

    def _resolve_outbound_workspace_path(self, source: str) -> Optional[Path]:
        workspace_root = self._agent_workspace_root()
        if workspace_root is None:
            return None
        source = source.strip().strip("<>")
        relative_path = _workspace_blob_relative_path(source)
        if relative_path:
            candidate = (workspace_root / relative_path).resolve()
        else:
            parsed = urlparse(source)
            if parsed.scheme and parsed.scheme != "file":
                return None
            raw_path = unquote(parsed.path if parsed.scheme == "file" else source)
            path = Path(raw_path).expanduser()
            candidate = path.resolve() if path.is_absolute() else (workspace_root / raw_path).resolve()
        if not candidate.is_relative_to(workspace_root) or not candidate.is_file():
            return None
        return candidate

    def _path_to_workspace_blob_url(self, path: Path) -> str:
        workspace_root = self._agent_workspace_root()
        if workspace_root is None:
            return ""
        resolved = path.expanduser().resolve()
        if not resolved.is_relative_to(workspace_root):
            return ""
        return workspace_blob_url(resolved.relative_to(workspace_root).as_posix())

    def _agent_workspace_root(self) -> Optional[Path]:
        workspace_dir = getattr(self.agent, "workspace_dir", None)
        if workspace_dir is None:
            return None
        return Path(workspace_dir).expanduser().resolve()

    @staticmethod
    def _is_outbound_image(path: Path) -> bool:
        mime_type, _ = mimetypes.guess_type(path.name)
        return bool(mime_type and mime_type.startswith("image/"))

    @staticmethod
    def _format_message(content: str) -> str:
        return str(content or "").strip()


def _is_error(ret: Any, errcode: Any) -> bool:
    return ret not in (None, 0, "0") or errcode not in (None, 0, "0")


def _error_code(ret: Any, errcode: Any) -> Optional[int]:
    for value in (errcode, ret):
        if value in (None, 0, "0"):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _safe_id(value: Any, keep: int = 8) -> str:
    text = str(value or "").strip()
    if len(text) <= keep:
        return text or "?"
    return text[:keep]


def _safe_filename_part(value: str) -> str:
    text = _SAFE_FILENAME_RE.sub("-", str(value or "").strip()).strip(".-_")
    return text[:48] or "weixin-media"


def _image_extension(mime_type: str, file_name: str = "") -> str:
    if mime_type == "image/jpeg":
        return "jpg"
    if mime_type.startswith("image/"):
        return mime_type.split("/", 1)[1].replace("jpeg", "jpg")
    suffix = Path(file_name).suffix.lower().lstrip(".")
    return suffix if suffix in {"png", "jpg", "jpeg", "gif", "webp", "bmp"} else "jpg"


def _media_failure_note(item: dict[str, Any]) -> str:
    item_type = int(item.get("type") or 0)
    if item_type == ITEM_IMAGE:
        return "[Received image - download failed]"
    if item_type == ITEM_VIDEO:
        return "[Received video - download failed]"
    if item_type == ITEM_FILE:
        file_name = str((item.get("file_item") or {}).get("file_name") or "file")
        return f"[Received file: {file_name} - download failed]"
    if item_type == ITEM_VOICE:
        return "[Received voice message - download failed]"
    return "[Received media - download failed]"


def _workspace_blob_relative_path(source: str) -> str:
    parsed = urlparse(source)
    if parsed.path != "/api/workspace/blob":
        return ""
    values = parse_qs(parsed.query).get("path") or []
    return unquote(values[0]).strip("/") if values else ""


def _remove_spans(text: str, spans: list[tuple[int, int]]) -> str:
    pieces: list[str] = []
    last_index = 0
    for start, end in sorted(spans):
        pieces.append(text[last_index:start])
        last_index = end
    pieces.append(text[last_index:])
    return re.sub(r"\n{3,}", "\n\n", "".join(pieces)).strip()


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default
