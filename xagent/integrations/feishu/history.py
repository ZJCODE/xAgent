"""Pull recent group context from Feishu Open APIs.

The adapter only needs one operation for Phase 1: when the bot is mentioned
in a group/topic, read a small window of recent messages so the reply is
grounded in the current conversation. Missing scopes, missing SDK attributes,
or transport errors yield an empty result rather than raising.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Iterable, Optional

from ...core.formatters import (
    RoomContextEntry,
    format_room_context as format_structured_room_context,
    format_room_context_body,
    format_room_context_entry,
    format_room_context_timestamp,
    sanitize_room_context_field,
)
from .users import FEISHU_USER_FALLBACK_NAME, FeishuUserResolver, extract_feishu_id, safe_display_name


@dataclass(frozen=True)
class FeishuMessageRecord:
    """A normalized snapshot of one Feishu message."""

    message_id: str
    sender_id: str
    sender_name: Optional[str]
    text: str
    create_time_ms: int
    source: str = "chat"  # "chat" | "thread"
    sender_type: Optional[str] = None
    sender_id_type: Optional[str] = None


_TEXTUAL_TYPES = {"text", "post"}


class FeishuHistoryFetcher:
    """Fetch recent group/topic context for an inbound Feishu message.

    The fetcher is a thin wrapper over the ``im.v1.message.alist`` OpenAPI;
    it normalizes responses into
    :class:`FeishuMessageRecord` values ordered oldest -> newest.
    """

    _MAX_PAGE_SIZE = 50  # Feishu API hard cap

    def __init__(
        self,
        channel: Any,
        logger: Optional[logging.Logger] = None,
        *,
        user_resolver: Optional[FeishuUserResolver] = None,
    ) -> None:
        self._channel = channel
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._user_resolver = user_resolver or FeishuUserResolver(channel, self._logger)

    async def fetch_recent_messages(
        self,
        *,
        chat_id: str,
        current_message_id: Optional[str],
        thread_id: Optional[str] = None,
        fetch_limit: int = 0,
    ) -> list[FeishuMessageRecord]:
        """Return recent group/topic messages ordered oldest -> newest.

        The triggering message (``current_message_id``) is always excluded.
        """
        if fetch_limit <= 0:
            return []

        container_id_type = "thread" if thread_id else "chat"
        container_id = thread_id or chat_id
        if not container_id:
            return []

        records = await self._list_messages(
            container_id_type,
            container_id,
            fetch_limit,
            source=container_id_type,
        )
        if current_message_id:
            records = [rec for rec in records if rec.message_id != current_message_id]
        return sorted(records, key=lambda r: r.create_time_ms)

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
    ) -> list[FeishuMessageRecord]:
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
        normalized: list[FeishuMessageRecord] = []
        for item in items:
            rec = self._normalize_item(item, source=source)
            if rec is not None:
                normalized.append(rec)
        return await self._resolve_record_names(normalized)

    async def _resolve_record_names(self, records: list[FeishuMessageRecord]) -> list[FeishuMessageRecord]:
        resolved_records: list[FeishuMessageRecord] = []
        for record in records:
            sender_name = await self._user_resolver.resolve_name(
                record.sender_id,
                fallback=record.sender_name,
                id_type=record.sender_id_type,
                sender_type=record.sender_type,
            )
            if sender_name != record.sender_name:
                record = replace(record, sender_name=sender_name)
            resolved_records.append(record)
        return resolved_records

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    @classmethod
    def _normalize_item(
        cls,
        item: Any,
        *,
        source: str,
    ) -> Optional[FeishuMessageRecord]:
        get = cls._attr_getter(item)
        message_id = get("message_id")
        if not message_id:
            return None
        if bool(get("deleted")):
            return None
        sender = get("sender")
        sender_get = cls._attr_getter(sender) if sender is not None else (lambda _k: None)
        sender_id, extracted_sender_id_type = extract_feishu_id(sender)
        if not sender_id:
            sender_id, extracted_sender_id_type = extract_feishu_id(get("sender_id"))
        sender_name = cls._first_present(
            sender_get("name"),
            sender_get("sender_name"),
            sender_get("user_name"),
            sender_get("display_name"),
            sender_get("app_name"),
            sender_get("bot_name"),
        )
        sender_type = cls._first_present(sender_get("sender_type"), get("sender_type"))
        sender_id_type = cls._first_present(
            sender_get("id_type"),
            sender_get("user_id_type"),
            sender_get("sender_id_type"),
            get("sender_id_type"),
            get("user_id_type"),
            extracted_sender_id_type,
        )
        msg_type = get("msg_type") or ""
        mentions = get("mentions") or []
        body = get("body")
        body_content = cls._attr_getter(body)("content") if body is not None else None
        text = replace_mentions(
            cls._render_content(msg_type, body_content),
            mentions,
        ).strip()
        if not text:
            return None
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
            sender_type=str(sender_type).lower() if sender_type else None,
            sender_id_type=str(sender_id_type).lower() if sender_id_type else None,
        )

    @staticmethod
    def _first_present(*values: Any) -> Optional[str]:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

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


def format_group_history(
    records: Iterable[FeishuMessageRecord],
    *,
    bot_open_id: Optional[str] = None,
    bot_app_id: Optional[str] = None,
) -> str:
    """Render recent Feishu messages as compact room-context lines."""
    entries = _build_room_context_entries(
        records,
        bot_open_id=bot_open_id,
        bot_app_id=bot_app_id,
    )
    return format_room_context_body(entries)


def format_sender_label(
    sender_name: Optional[str],
    sender_id: Optional[str],
    *,
    sender_type: Optional[str] = None,
) -> str:
    """Render a transcript speaker with sender ID appended when available."""
    safe_name = safe_display_name(sender_name)
    safe_id = sanitize_sender_id(sender_id)
    fallback_name = fallback_sender_label(sender_type)
    if safe_name and safe_id:
        return f"{safe_name}({safe_id})"
    if safe_name:
        return safe_name
    if safe_id:
        return f"{fallback_name}({safe_id})"
    return fallback_name


def fallback_sender_label(sender_type: Optional[str]) -> str:
    normalized = (sender_type or "").strip().lower()
    if normalized in {"app", "bot"}:
        return "Feishu Bot"
    return FEISHU_USER_FALLBACK_NAME


def format_room_context(
    room_id: str,
    records: Iterable[FeishuMessageRecord],
    *,
    room_name: Optional[str] = None,
    bot_open_id: Optional[str] = None,
    bot_app_id: Optional[str] = None,
) -> str:
    """Render a Feishu group/topic context block for ``agent.chat``."""
    entries = _build_room_context_entries(
        records,
        bot_open_id=bot_open_id,
        bot_app_id=bot_app_id,
    )
    return format_structured_room_context(room_id, entries, room_name=room_name)


def format_room_context_line(speaker: str, create_time_ms: int, text: str) -> str:
    """Return one compact room-context line."""
    safe_speaker = sanitize_transcript_field(speaker) or FEISHU_USER_FALLBACK_NAME
    entry = RoomContextEntry(
        speaker_label=safe_speaker,
        occurred_at=_feishu_timestamp_to_datetime(create_time_ms),
        text=text,
    )
    return format_room_context_entry(entry) or ""


def format_feishu_timestamp(create_time_ms: int) -> str:
    """Format a Feishu timestamp for compact room-context lines."""
    return format_room_context_timestamp(_feishu_timestamp_to_datetime(create_time_ms))


def replace_mentions(
    text: str,
    mentions: Iterable[Any],
) -> str:
    """Replace Feishu mention keys such as ``@_user_1`` with display names."""
    rendered = text or ""
    for mention in mentions or []:
        key = _mention_attr(mention, "key")
        name = safe_display_name(
            _mention_attr(mention, "name")
            or _mention_attr(mention, "user_name")
            or _mention_attr(mention, "display_name")
        )
        mention_id, _mention_id_type = extract_feishu_id(mention)
        if not key:
            continue
        if not name:
            if not mention_id:
                continue
            name = mention_id
        replacement = name if name.startswith("@") else f"@{name}"
        safe_mention_id = sanitize_sender_id(mention_id)
        normalized_name = name[1:] if name.startswith("@") else name
        if safe_mention_id and safe_mention_id != normalized_name:
            replacement = f"{replacement}({safe_mention_id})"
        rendered = rendered.replace(key, replacement)
    return rendered


def sanitize_transcript_field(value: Optional[str]) -> Optional[str]:
    return sanitize_room_context_field(value)


def sanitize_sender_id(value: Optional[str]) -> Optional[str]:
    return sanitize_transcript_field(value)


def _mention_attr(mention: Any, field_name: str) -> Optional[str]:
    if isinstance(mention, dict):
        value = mention.get(field_name)
    else:
        value = getattr(mention, field_name, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _build_room_context_entries(
    records: Iterable[FeishuMessageRecord],
    *,
    bot_open_id: Optional[str] = None,
    bot_app_id: Optional[str] = None,
) -> list[RoomContextEntry]:
    bot_ids = {value for value in (bot_open_id, bot_app_id) if value}
    entries: list[RoomContextEntry] = []
    for record in records:
        text = record.text or ""
        if not text.strip():
            continue
        is_self = record.sender_id in bot_ids or record.sender_name in bot_ids
        speaker_label = "ME"
        if not is_self:
            speaker_label = format_sender_label(
                record.sender_name,
                record.sender_id,
                sender_type=record.sender_type,
            )
        entries.append(
            RoomContextEntry(
                speaker_label=speaker_label,
                occurred_at=_feishu_timestamp_to_datetime(record.create_time_ms),
                text=text,
                is_self=is_self,
            )
        )
    return entries


def _feishu_timestamp_to_datetime(create_time_ms: int) -> datetime:
    if create_time_ms <= 0:
        return datetime.fromtimestamp(0)
    seconds = create_time_ms / 1000 if create_time_ms > 10_000_000_000 else create_time_ms
    return datetime.fromtimestamp(seconds)
