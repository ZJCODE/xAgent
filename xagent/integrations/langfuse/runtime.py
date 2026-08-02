"""Langfuse v4 observability runtime for OpenAI-compatible clients."""

from __future__ import annotations

import inspect
import json
import logging
from contextlib import contextmanager
from typing import Any, ContextManager, Iterator, Optional, Protocol

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)

_MAX_PROPAGATED_CHARS = 200
_MAX_IO_CHARS = 4000


# ---------------------------------------------------------------------------
# Observation helpers
# ---------------------------------------------------------------------------

class _TurnObservation:
    """Active agent-turn observation handle."""

    def __init__(self) -> None:
        self.span: Any = None

    def set_input(self, messages: list) -> None:
        if self.span is None:
            return
        try:
            self.span.update(input=_turn_input_payload(messages))
        except Exception:
            pass

    def set_output(self, content: str) -> None:
        if self.span is None:
            return
        try:
            self.span.update(output={"content": _truncate(content, _MAX_IO_CHARS)})
        except Exception:
            pass

    def set_error(
        self,
        *,
        error_id: str,
        code: str,
        message: str,
    ) -> None:
        if self.span is None:
            return
        try:
            self.span.update(
                level="ERROR",
                status_message=_truncate(message, _MAX_PROPAGATED_CHARS),
                output={
                    "error_id": error_id,
                    "error_code": code,
                    "error": _truncate(message, _MAX_IO_CHARS),
                },
                metadata=_string_metadata(
                    {
                        "error_id": error_id,
                        "error_code": code,
                    }
                ),
            )
        except Exception:
            pass


class _ToolObservation:
    """Active tool-call observation handle."""

    def __init__(self) -> None:
        self.span: Any = None

    def set_output(self, content: Any) -> None:
        if self.span is None:
            return
        try:
            self.span.update(output=_tool_payload(content))
        except Exception:
            pass

    def set_error(self, message: str) -> None:
        if self.span is None:
            return
        try:
            self.span.update(
                level="ERROR",
                status_message=_truncate(message, _MAX_PROPAGATED_CHARS),
                output={"error": _truncate(message, _MAX_IO_CHARS)},
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def build_session_id(
    *,
    channel: str = "",
    room_name: Optional[str] = None,
    user_id: str = "",
) -> str:
    """Build a stable Langfuse session id for one conversation thread."""
    channel_part = str(channel or "local").strip() or "local"
    peer = str(room_name or user_id or "anonymous").strip() or "anonymous"
    return _truncate(f"{channel_part}:{peer}", _MAX_PROPAGATED_CHARS)


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


def _string_metadata(values: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key, raw in values.items():
        key_text = "".join(ch for ch in str(key) if ch.isalnum() or ch == "_")
        if not key_text:
            continue
        value = _truncate(raw, _MAX_PROPAGATED_CHARS)
        if value:
            metadata[key_text] = value
    return metadata


def _message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text")
                if text:
                    parts.append(str(text))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def _turn_input_payload(messages: list) -> dict[str, Any]:
    if not messages:
        return {"total": 0}

    roles: dict[str, int] = {}
    latest_user = ""
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "unknown")
        roles[role] = roles.get(role, 0) + 1
        if role == "user":
            text = _message_text(message).strip()
            if text:
                latest_user = text

    payload: dict[str, Any] = {
        "total": len(messages),
        "roles": roles,
    }
    if latest_user:
        payload["user"] = _truncate(latest_user, _MAX_IO_CHARS)
    return payload


def _tool_payload(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
        return {"preview": _truncate(text, _MAX_IO_CHARS)}
    return {"content": _truncate(value, _MAX_IO_CHARS)}


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class ObservabilityRuntime(Protocol):
    """Interface used by the agent runtime for optional tracing."""

    enabled: bool

    def create_client(self, client_kwargs: dict[str, Any]) -> Optional[AsyncOpenAI]:
        """Create an OpenAI-compatible async client (possibly wrapped)."""

    def agent_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        model: str,
        channel: str,
        stream: bool,
    ) -> ContextManager[_TurnObservation]:
        """Return a context manager that wraps one agent chat turn."""

    def tool_call(
        self,
        *,
        name: str,
        call_id: str,
        arguments: Any,
    ) -> ContextManager[_ToolObservation]:
        """Return a context manager that wraps one tool execution."""

    async def flush(self) -> None:
        """Flush queued observability events."""


# ---------------------------------------------------------------------------
# Noop runtime
# ---------------------------------------------------------------------------

class NoopObservabilityRuntime:
    """Default observability runtime — all operations are no-ops."""

    enabled = False

    def create_client(self, client_kwargs: dict[str, Any]) -> Optional[AsyncOpenAI]:
        if not client_kwargs:
            return None
        return AsyncOpenAI(**client_kwargs)

    @contextmanager
    def agent_turn(
        self,
        *,
        user_id: str = "",
        session_id: str = "",
        model: str = "",
        channel: str = "",
        stream: bool = False,
    ) -> Iterator[_TurnObservation]:
        yield _TurnObservation()

    @contextmanager
    def tool_call(
        self,
        *,
        name: str = "",
        call_id: str = "",
        arguments: Any = None,
    ) -> Iterator[_ToolObservation]:
        yield _ToolObservation()

    async def flush(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Langfuse-backed runtime
# ---------------------------------------------------------------------------

class LangfuseObservabilityRuntime:
    """Langfuse-backed observability runtime (Python SDK v4)."""

    enabled = True

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self._client: Any = None

    def create_client(self, client_kwargs: dict[str, Any]) -> AsyncOpenAI:
        self._ensure_langfuse_client()
        from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI

        return LangfuseAsyncOpenAI(**client_kwargs)

    @contextmanager
    def agent_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        model: str,
        channel: str,
        stream: bool,
    ) -> Iterator[_TurnObservation]:
        observation = _TurnObservation()
        try:
            from langfuse import propagate_attributes

            langfuse_client = self._ensure_langfuse_client()
            tags = [
                "xagent",
                "chat",
                f"model:{model}" if model else "model:unknown",
                f"channel:{channel}" if channel else "channel:local",
                "stream" if stream else "non-stream",
            ]
            metadata = _string_metadata(
                {
                    "channel": channel or "local",
                    "model": model,
                }
            )
        except Exception as exc:
            logger.warning("Failed to initialize Langfuse observation: %s", exc)
            yield observation
            return

        with langfuse_client.start_as_current_observation(
            as_type="agent",
            name="xagent.chat",
        ) as span:
            observation.span = span
            with propagate_attributes(
                user_id=_truncate(user_id, _MAX_PROPAGATED_CHARS) or None,
                session_id=_truncate(session_id, _MAX_PROPAGATED_CHARS) or None,
                tags=tags,
                metadata=metadata or None,
            ):
                yield observation

    @contextmanager
    def tool_call(
        self,
        *,
        name: str,
        call_id: str,
        arguments: Any,
    ) -> Iterator[_ToolObservation]:
        observation = _ToolObservation()
        try:
            langfuse_client = self._ensure_langfuse_client()
            tool_name = str(name or "tool").strip() or "tool"
        except Exception as exc:
            logger.warning("Failed to initialize Langfuse tool observation: %s", exc)
            yield observation
            return

        with langfuse_client.start_as_current_observation(
            as_type="tool",
            name=tool_name,
            input=_tool_payload(arguments),
            metadata=_string_metadata({"call_id": call_id}),
        ) as span:
            observation.span = span
            yield observation

    async def flush(self) -> None:
        try:
            client = self._client
            if client is None:
                return
            flush = getattr(client, "flush", None)
            if flush is None:
                return
            result = flush()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("Failed to flush Langfuse events: %s", exc)

    def _ensure_langfuse_client(self) -> Any:
        if self._client is not None:
            return self._client

        from langfuse import Langfuse

        kwargs: dict[str, Any] = {
            "public_key": str(self.config.get("public_key") or "").strip() or None,
            "secret_key": str(self.config.get("secret_key") or "").strip() or None,
        }
        base_url = self.config.get("base_url")
        if isinstance(base_url, str) and base_url.strip():
            kwargs["base_url"] = base_url.strip()
        if "sample_rate" in self.config:
            kwargs["sample_rate"] = float(self.config["sample_rate"])
        if "debug" in self.config:
            kwargs["debug"] = bool(self.config["debug"])
        if "tracing_enabled" in self.config:
            kwargs["tracing_enabled"] = bool(self.config["tracing_enabled"])
        if "environment" in self.config:
            environment = str(self.config.get("environment") or "").strip()
            if environment:
                kwargs["environment"] = environment
        if "release" in self.config:
            release = str(self.config.get("release") or "").strip()
            if release:
                kwargs["release"] = release

        self._client = Langfuse(**{key: value for key, value in kwargs.items() if value is not None})
        return self._client


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_observability_runtime(config: Optional[dict[str, Any]]) -> ObservabilityRuntime:
    if not isinstance(config, dict) or not config.get("enabled"):
        return NoopObservabilityRuntime()

    provider = str(config.get("provider") or "").strip().lower()
    if provider != "langfuse":
        return NoopObservabilityRuntime()

    try:
        return LangfuseObservabilityRuntime(config)
    except Exception as exc:
        logger.warning("Failed to initialize Langfuse observability runtime: %s", exc)
        return NoopObservabilityRuntime()
