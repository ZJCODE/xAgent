"""Outbound text helpers for the Weixin adapter."""
from __future__ import annotations

import re
import uuid
from typing import Any


def make_client_id(prefix: str = "xagent-weixin", stable_key: str = "") -> str:
    if stable_key:
        safe_key = re.sub(r"[^A-Za-z0-9._:-]+", "-", stable_key).strip("-")[:120]
        return f"{prefix}:{safe_key}" if safe_key else f"{prefix}:{uuid.uuid4().hex}"
    return f"{prefix}:{uuid.uuid4().hex}"


def split_text(text: str, max_chars: int = 2000) -> list[str]:
    content = str(text or "")
    if not content:
        return []
    limit = max(1, int(max_chars or 2000))
    if len(content) <= limit:
        return [content]

    chunks: list[str] = []
    remaining = content
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        break_at = _best_break(remaining, limit)
        chunks.append(remaining[:break_at].rstrip())
        remaining = remaining[break_at:].lstrip("\n")
    return [chunk for chunk in chunks if chunk]


def _best_break(text: str, limit: int) -> int:
    for marker in ("\n\n", "\n", " "):
        index = text.rfind(marker, 0, limit + 1)
        if index > 0:
            return index + (0 if marker.startswith("\n") else 1)
    return limit


def extract_text(item_list: list[dict[str, Any]]) -> str:
    for item in item_list:
        if int(item.get("type") or 0) == 1:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_text = extract_text([ref_item]) if isinstance(ref_item, dict) and ref_item else ""
            title = str(ref.get("title") or "").strip()
            if title or ref_text:
                parts = [part for part in (title, ref_text) if part]
                return f"[quote: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if int(item.get("type") or 0) == 3:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""
