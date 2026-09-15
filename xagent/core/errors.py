"""Public chat error surface: safe client payload + correlatable diagnostics."""

from __future__ import annotations

import logging
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)

# Stable public taxonomy exposed to clients.
ERROR_INVALID_INPUT = "invalid_input"
ERROR_AUTH_FAILED = "auth_failed"
ERROR_QUOTA_EXCEEDED = "quota_exceeded"
ERROR_MODEL_NOT_FOUND = "model_not_found"
ERROR_NETWORK = "network_error"
ERROR_MODEL_UNAVAILABLE = "model_unavailable"
ERROR_EMPTY_RESPONSE = "empty_response"
ERROR_TURN_EXHAUSTED = "turn_exhausted"
ERROR_TIMEOUT = "timeout"
ERROR_CAPACITY = "capacity"
ERROR_INTERNAL = "internal"

DEFAULT_MESSAGES: Mapping[str, str] = {
    ERROR_INVALID_INPUT: "Invalid input. Please check your request and try again.",
    ERROR_AUTH_FAILED: "The provider rejected the API key. Check provider.api_key in config.yaml.",
    ERROR_QUOTA_EXCEEDED: "The provider reported a quota or rate limit. Check your account balance.",
    ERROR_MODEL_NOT_FOUND: "The provider does not recognize this model. Check provider.model.",
    ERROR_NETWORK: "Could not reach the provider. Check provider.base_url and your network.",
    ERROR_MODEL_UNAVAILABLE: "Model request failed. Please try again.",
    ERROR_EMPTY_RESPONSE: "The model returned no response. Please try again.",
    ERROR_TURN_EXHAUSTED: "Sorry, I could not generate a response after multiple attempts.",
    ERROR_TIMEOUT: "Agent chat timed out.",
    ERROR_CAPACITY: "Too many concurrent chat requests; try again later.",
    ERROR_INTERNAL: "Sorry, I encountered an error while processing your request.",
}

STATUS_BY_CODE: Mapping[str, int] = {
    ERROR_INVALID_INPUT: 400,
    ERROR_AUTH_FAILED: 401,
    ERROR_QUOTA_EXCEEDED: 429,
    ERROR_MODEL_NOT_FOUND: 404,
    ERROR_NETWORK: 502,
    ERROR_MODEL_UNAVAILABLE: 502,
    ERROR_EMPTY_RESPONSE: 502,
    ERROR_TURN_EXHAUSTED: 503,
    ERROR_TIMEOUT: 504,
    ERROR_CAPACITY: 429,
    ERROR_INTERNAL: 500,
}

_MODEL_CODE_MAP: Mapping[str, str] = {
    "model_auth_failed": ERROR_AUTH_FAILED,
    "model_quota_exceeded": ERROR_QUOTA_EXCEEDED,
    "model_not_found": ERROR_MODEL_NOT_FOUND,
    "model_network_error": ERROR_NETWORK,
    "model_call_failed": ERROR_MODEL_UNAVAILABLE,
    "model_stream_failed": ERROR_MODEL_UNAVAILABLE,
    "model_stream_error": ERROR_MODEL_UNAVAILABLE,
    "empty_model_response": ERROR_EMPTY_RESPONSE,
    "empty_stream_response": ERROR_EMPTY_RESPONSE,
}

RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def new_error_id() -> str:
    """Return a short correlatable token shared by client message and logs."""
    return uuid.uuid4().hex[:8]


def map_model_error_code(model_code: Optional[str]) -> str:
    """Map an internal ModelErrorEvent.code onto the public taxonomy."""
    if not model_code:
        return ERROR_MODEL_UNAVAILABLE
    return _MODEL_CODE_MAP.get(str(model_code), ERROR_MODEL_UNAVAILABLE)


def model_http_status(exc: BaseException) -> Optional[int]:
    """Best-effort HTTP status from OpenAI/Anthropic/httpx-style exceptions."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(exc, "response", None)
    nested = getattr(response, "status_code", None) if response is not None else None
    return nested if isinstance(nested, int) else None


def model_error_code_from_exception(exc: BaseException) -> str:
    """Map a provider SDK exception onto a stable internal model error code."""
    status = model_http_status(exc)
    if status in (401, 403):
        return "model_auth_failed"
    if status in (402, 429):
        return "model_quota_exceeded"
    if status == 404:
        return "model_not_found"
    if status is None and _looks_like_network_error(exc):
        return "model_network_error"
    return "model_call_failed"


def is_retryable_model_exception(exc: BaseException) -> bool:
    """Return whether tenacity should retry this provider exception."""
    status = model_http_status(exc)
    if status is not None:
        return status in RETRYABLE_HTTP_STATUS_CODES
    return _looks_like_network_error(exc)


def _looks_like_network_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    typename = type(exc).__name__.lower()
    return any(hint in typename for hint in ("connect", "timeout", "network"))


def map_model_error(error: Any) -> str:
    """Map a ModelErrorEvent (or similar) onto a public error code."""
    code = getattr(error, "code", None)
    if code is None and isinstance(error, Mapping):
        code = error.get("code")
    return map_model_error_code(str(code) if code is not None else None)


def build_public_error(
    *,
    code: str,
    status_code: Optional[int] = None,
    message: Optional[str] = None,
    cause: Any = None,
    log: bool = True,
) -> dict[str, Any]:
    """Build a client-safe chat error event and log the internal cause.

    The ``error`` string always embeds ``error_id=...`` so IM channels that only
    render the text still carry a grep-able correlation token.
    """
    public_code = code if code in DEFAULT_MESSAGES else ERROR_INTERNAL
    error_id = new_error_id()
    base = (message or DEFAULT_MESSAGES[public_code]).strip() or DEFAULT_MESSAGES[public_code]
    public_message = f"{base} (error_id={error_id})"
    resolved_status = STATUS_BY_CODE.get(public_code) if status_code is None else status_code

    payload: dict[str, Any] = {
        "type": "error",
        "error": public_message,
        "error_code": public_code,
        "error_id": error_id,
    }
    if resolved_status is not None:
        payload["status_code"] = resolved_status

    if log:
        logger.error(
            "Chat error error_id=%s code=%s cause=%r",
            error_id,
            public_code,
            cause,
        )
    return payload


class PublicChatError(Exception):
    """Carry a public error payload across HTTP and WebSocket boundaries."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(str(payload.get("error") or "Chat error"))

    @property
    def status_code(self) -> int:
        return int(self.payload.get("status_code") or STATUS_BY_CODE[ERROR_INTERNAL])

    @property
    def detail(self) -> str:
        return str(self.payload.get("error") or "Chat error")
