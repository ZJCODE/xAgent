"""Pull recent chat context from Feishu Open APIs.

Feishu only pushes the bot messages it is allowed to see (DMs, @-mentions,
group-message events). To behave like a teammate that "scrolls up before
replying", the adapter calls :class:`FeishuHistoryFetcher` to retrieve the
surrounding context (the replied-to message, topic/thread siblings, and
recent group history) and feeds the result into ``agent.observe`` before
``agent.chat``.

All API calls degrade gracefully: missing scopes, missing SDK attributes, or
transport errors yield an empty result rather than raising.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional


@dataclass(frozen=True)
class FeishuMessageRecord:
    """A normalized snapshot of one Feishu message."""

    message_id: str
    sender_id: str
    sender_name: Optional[str]
    text: str
    create_time_ms: int
    source: str  # "parent" | "thread" | "history"


_TEXTUAL_TYPES = {"text", "post"}


class FeishuHistoryFetcher:
    """Fetch surrounding context for an inbound Feishu message.

    The fetcher is a thin wrapper over ``channel.fetch_message`` and the
    ``im.v1.message.alist`` OpenAPI; it normalizes responses into
    :class:`FeishuMessageRecord` and dedupes/sorts results.
    """

    _MAX_PAGE_SIZE = 50  # Feishu API hard cap

    def __init__(self, channel: Any, logger: Optional[logging.Logger] = None) -> None:
        self._channel = channel
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    async def fetch_context(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        parent_message_id: Optional[str] = None,
        thread_id: Optional[str] = None,
        history_count: int = 0,
    ) -> List[FeishuMessageRecord]:
        """Return recent context messages ordered oldest -> newest.

        The triggering message (``current_message_id``) is always excluded.
        """
        records: dict[str, FeishuMessageRecord] = {}

        tasks: list[asyncio.Task[List[FeishuMessageRecord]]] = []
        if parent_message_id:
            tasks.append(asyncio.create_task(self._get_message(parent_message_id, "parent")))
        if thread_id:
            tasks.append(
                asyncio.create_task(
                    self._list_messages("thread", thread_id, history_count or 20, source="thread")
                )
            )
        if history_count > 0:
            tasks.append(
                asyncio.create_task(
                    self._list_messages("chat", chat_id, history_count, source="history")
                )
            )

        for batch in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(batch, BaseException):
                self._logger.debug("Feishu history fetch raised: %s", batch)
                continue
            for rec in batch:
                # Earlier sources win (parent > thread > history).
                records.setdefault(rec.message_id, rec)

        if current_message_id:
            records.pop(current_message_id, None)

        return sorted(records.values(), key=lambda r: r.create_time_ms)

    # ------------------------------------------------------------------
    # Single-message fetch
    # ------------------------------------------------------------------

    async def _get_message(self, message_id: str, source: str) -> List[FeishuMessageRecord]:
        fetch = getattr(self._channel, "fetch_message", None)
        if fetch is None:
            return []
        try:
            payload = await fetch(message_id)
        except Exception as exc:
            self._logger.info("Feishu fetch_message(%s) failed: %s", message_id, exc)
            return []
        item = self._extract_first_item(payload)
        if item is None:
            return []
        rec = self._normalize_item(item, source=source)
        return [rec] if rec else []

    @staticmethod
    def _extract_first_item(payload: Any) -> Optional[dict]:
        if not isinstance(payload, dict):
            return None
        data = payload.get("data") or {}
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            return items[0]
        if isinstance(data, dict) and data.get("message_id"):
            return data
        return None

    # ------------------------------------------------------------------
    # List fetch
    # ------------------------------------------------------------------

    async def _list_messages(
        self,
        container_id_type: str,
        container_id: str,
        page_size: int,
        *,
        source: str,
    ) -> List[FeishuMessageRecord]:
        client = getattr(self._channel, "client", None)
        if client is None:
            return []
        try:
            from lark_oapi.api.im.v1 import ListMessageRequest  # type: ignore
        except ImportError:  # pragma: no cover - import guard
            return []

        request = (
            ListMessageRequest.builder()
            .container_id_type(container_id_type)
            .container_id(container_id)
            .page_size(max(1, min(page_size, self._MAX_PAGE_SIZE)))
            .sort_type("ByCreateTimeDesc")
            .build()
        )

        try:
            response = await client.im.v1.message.alist(request)
        except Exception as exc:
            self._logger.info(
                "Feishu list messages failed (container=%s/%s): %s",
                container_id_type,
                container_id,
                exc,
            )
            return []

        if not getattr(response, "success", lambda: False)():
            self._logger.info(
                "Feishu list messages rejected (container=%s/%s): code=%s msg=%s",
                container_id_type,
                container_id,
                getattr(response, "code", None),
                getattr(response, "msg", None),
            )
            return []

        items = getattr(getattr(response, "data", None), "items", None) or []
        normalized: List[FeishuMessageRecord] = []
        for item in items:
            rec = self._normalize_item(item, source=source)
            if rec is not None:
                normalized.append(rec)
        return normalized

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @classmethod
    def _normalize_item(cls, item: Any, *, source: str) -> Optional[FeishuMessageRecord]:
        get = cls._attr_getter(item)
        message_id = get("message_id")
        if not message_id:
            return None
        sender = get("sender")
        sender_get = cls._attr_getter(sender) if sender is not None else (lambda _k: None)
        sender_id = sender_get("id") or sender_get("sender_id") or ""
        sender_name = sender_get("name")
        msg_type = get("msg_type") or ""
        body = get("body")
        body_content = cls._attr_getter(body)("content") if body is not None else None
        text = cls._render_content(msg_type, body_content)
        create_time_raw = get("create_time") or 0
        try:
            create_time_ms = int(create_time_raw)
        except (TypeError, ValueError):
            create_time_ms = 0
        return FeishuMessageRecord(
            message_id=str(message_id),
            sender_id=str(sender_id or ""),
            sender_name=str(sender_name) if sender_name else None,
            text=text,
            create_time_ms=create_time_ms,
            source=source,
        )

    @staticmethod
    def _attr_getter(obj: Any):
        if obj is None:
            return lambda _k: None
        if isinstance(obj, dict):
            return obj.get
        return lambda k, _o=obj: getattr(_o, k, None)

    @classmethod
    def _render_content(cls, msg_type: str, raw_content: Any) -> str:
        if raw_content is None:
            return f"[{msg_type or 'message'}]"
        if isinstance(raw_content, str):
            try:
                payload = json.loads(raw_content)
            except (TypeError, ValueError):
                return raw_content.strip()
        else:
            payload = raw_content
        if msg_type in _TEXTUAL_TYPES or msg_type == "":
            text = cls._extract_text(payload)
            if text:
                return text
        return f"[{msg_type or 'message'}]"

    @classmethod
    def _extract_text(cls, payload: Any) -> str:
        if isinstance(payload, str):
            return payload.strip()
        if not isinstance(payload, dict):
            return ""
        if isinstance(payload.get("text"), str):
            return payload["text"].strip()
        # Rich post: {"title": "...", "content": [[{"tag":"text","text":"..."}, ...]]}
        parts: list[str] = []
        title = payload.get("title")
        if isinstance(title, str) and title.strip():
            parts.append(title.strip())
        content = payload.get("content")
        if isinstance(content, list):
            for line in content:
                if isinstance(line, list):
                    line_parts: list[str] = []
                    for node in line:
                        if isinstance(node, dict):
                            tag = node.get("tag")
                            if tag in {"text", "md"} and isinstance(node.get("text"), str):
                                line_parts.append(node["text"])
                            elif tag == "a" and isinstance(node.get("text"), str):
                                line_parts.append(node["text"])
                            elif tag == "at" and isinstance(node.get("user_name"), str):
                                line_parts.append(f"@{node['user_name']}")
                    if line_parts:
                        parts.append("".join(line_parts))
        return "\n".join(p for p in parts if p).strip()


def format_context_recap(
    records: Iterable[FeishuMessageRecord],
    *,
    bot_open_id: Optional[str] = None,
) -> str:
    """Render a list of context messages as a transcript for ``agent.observe``."""
    lines: list[str] = []
    for rec in records:
        who = rec.sender_name or rec.sender_id or "unknown"
        if bot_open_id and rec.sender_id == bot_open_id:
            who = f"{who} (bot)"
        tag = f"[{rec.source}]"
        text = rec.text or ""
        lines.append(f"{tag} {who}: {text}".rstrip())
    return "\n".join(lines)
