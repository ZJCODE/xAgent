"""Outbound send helpers for the Feishu adapter.

Wraps :meth:`FeishuChannel.send` with a small recovery layer:

* When a ``reply_to`` anchor is missing/withdrawn/edited
  (``FeishuChannelErrorCode.TARGET_REVOKED``), automatically retry as a
  top-level send. This is only safe for **direct** chats — in groups
  (especially topic groups) a fallback could leak a private thread reply
  into the parent chat, so we keep the failure visible there.
* Normalize logging so every send failure surfaces ``raw_code`` /
  ``hint``.

The helper deliberately keeps the call surface tiny: the adapter passes
the payload and the anchor; this module decides whether to retry.
"""
from __future__ import annotations

import logging
from typing import Any, Optional


_TARGET_REVOKED_HINT_TOKENS = ("withdrawn", "not found", "recalled", "deleted")


def _is_target_revoked(result: Any) -> bool:
    """Return True iff ``result`` indicates the reply anchor is unusable."""
    if getattr(result, "success", True):
        return False
    error = getattr(result, "error", None)
    if error is None:
        return False
    code = getattr(error, "code", None)
    code_value = getattr(code, "value", code)
    if code_value == "target_revoked":
        return True
    hint = (getattr(error, "hint", "") or "").lower()
    return any(token in hint for token in _TARGET_REVOKED_HINT_TOKENS)


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


async def send_with_fallback(
    channel: Any,
    *,
    chat_id: str,
    payload: dict,
    reply_to: Optional[str],
    is_p2p: bool,
    logger: logging.Logger,
    message_id: Optional[str] = None,
) -> Any:
    """Send via ``channel.send``; retry without ``reply_to`` on revoked targets.

    Args:
        channel: A live ``FeishuChannel`` instance.
        chat_id: Target chat_id.
        payload: Send payload (e.g. ``{"markdown": "..."}``).
        reply_to: Optional anchor message id; pass ``None`` for fresh send.
        is_p2p: When True, allow falling back to a top-level send if the
            anchor is revoked. Group replies never fall back to avoid
            leaking thread replies into the parent chat.
        logger: Logger used for diagnostics.
        message_id: Inbound message id, used only for logging context.

    Returns:
        The ``SendResult`` of the final attempted send (success or failure).
    """
    opts = {"reply_to": reply_to} if reply_to else None
    result = await channel.send(chat_id, payload, opts)

    if getattr(result, "success", True):
        return result

    revoked = _is_target_revoked(result)
    if revoked and reply_to and is_p2p:
        logger.warning(
            "Feishu reply anchor revoked; retrying as fresh send: chat_id=%s reply_to=%s %s",
            chat_id,
            reply_to,
            _format_failure(result),
        )
        fresh = await channel.send(chat_id, payload, None)
        if not getattr(fresh, "success", True):
            logger.error(
                "Feishu fallback send failed: chat_id=%s message_id=%s %s",
                chat_id,
                message_id,
                _format_failure(fresh),
            )
        return fresh

    logger.error(
        "Feishu send failed: chat_id=%s reply_to=%s message_id=%s %s%s",
        chat_id,
        reply_to,
        message_id,
        _format_failure(result),
        " (target revoked; no group fallback)" if revoked else "",
    )
    return result
