"""Outbound send helper for the Feishu adapter.

The adapter decides whether a message is a fresh p2p send or a group reply.
This module only builds the small options payload (``reply_to`` and ``uuid``)
and normalizes failure logging.
"""
from __future__ import annotations

import logging
from typing import Any, Optional


def _format_failure(result: Any) -> str:
    error = getattr(result, "error", None)
    if error is None:
        return f"raw={getattr(result, 'raw', None)!r}"
    parts = []
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    if code_value is not None:
        parts.append(f"code={code_value}")
    raw_code = getattr(error, "raw_code", None)
    if raw_code is not None:
        parts.append(f"raw_code={raw_code}")
    hint = getattr(error, "hint", None)
    if hint:
        parts.append(f"hint={hint!r}")
    return " ".join(parts) or f"raw={getattr(result, 'raw', None)!r}"


async def send_message(
    channel: Any,
    *,
    chat_id: str,
    payload: dict,
    reply_to: Optional[str],
    uuid: Optional[str],
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> Any:
    """Send once via ``channel.send``.

    Args:
        channel: A live ``FeishuChannel`` instance.
        chat_id: Target chat_id.
        payload: Send payload (e.g. ``{"markdown": "..."}``).
        reply_to: Optional group anchor message id; pass ``None`` for fresh send.
        uuid: Optional Feishu request dedup id, derived from the inbound message id.
        logger: Logger used for diagnostics.
        message_id: Inbound message id, used only for logging context.

    Returns:
        The ``SendResult`` returned by ``channel.send``.
    """
    opts: dict[str, str] = {}
    if reply_to:
        opts["reply_to"] = reply_to
    if uuid:
        opts["uuid"] = uuid

    result = await channel.send(chat_id, payload, opts or None)

    if getattr(result, "success", True):
        return result

    logger.error(
        "Feishu send failed: chat_id=%s reply_to=%s uuid=%s message_id=%s %s",
        chat_id,
        reply_to,
        uuid,
        message_id,
        _format_failure(result),
    )
    return result
