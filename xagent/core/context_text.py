"""Shared text measurement and truncation helpers (no manifest/budget deps)."""

from __future__ import annotations

import math


def estimate_tokens(text: str) -> int:
    """Conservative heuristic: ASCII ~4 chars/token, CJK-heavy ~1.5 chars/token."""
    if not text:
        return 0
    ascii_n = sum(1 for char in text if ord(char) < 128)
    other_n = len(text) - ascii_n
    return int(math.ceil(ascii_n / 4 + other_n / 1.5))


def content_char_length(content: object) -> int:
    """Character length of a model message content field (str or multimodal list)."""
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict):
                total += len(str(part))
                continue
            part_type = part.get("type")
            if part_type == "text":
                total += len(str(part.get("text") or ""))
            elif part_type == "image_url":
                url = part.get("image_url") or {}
                if isinstance(url, dict):
                    total += len(str(url.get("url") or ""))
                else:
                    total += len(str(url))
            else:
                total += len(str(part))
        return total
    return len(str(content))


def truncate_middle(text: str, max_chars: int, *, notice: str = "[... middle omitted ...]") -> str:
    limit = max(0, int(max_chars))
    if limit <= 0 or len(text) <= limit:
        return text
    if limit <= len(notice) + 2:
        return text[:limit]
    head = max(1, (limit - len(notice)) // 2)
    tail = max(1, limit - len(notice) - head)
    return text[:head] + notice + text[-tail:]


def cap_message_content(text: str, max_chars: int) -> str:
    return truncate_middle(str(text or ""), max(1, int(max_chars)))
