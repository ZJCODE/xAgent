"""Optional Langfuse runtime wiring for OpenAI-compatible clients."""

from __future__ import annotations

import inspect
import logging
import os
from contextlib import contextmanager
from typing import Any, ContextManager, Optional, Protocol

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Observation object — yielded by agent_turn() so the agent can record
# input / output on the span after it is created.
# ---------------------------------------------------------------------------

class _TurnObservation:
    """Lightweight holder for the active span reference."""

    def __init__(self) -> None:
        self.span: Any = None

    def set_input(self, messages: list) -> None:
        if self.span is not None:
            try:
                self.span.update(input=_summarize_messages(messages))
            except Exception:
                pass

    def set_output(self, content: str) -> None:
        if self.span is not None:
            try:
                self.span.update(output={"content": content})
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _summarize_messages(messages: list) -> dict:
    """Return a compact summary of a message list for trace input metadata."""
    if not messages:
        return {"total": 0}
    roles: dict[str, int] = {}
    for m in messages:
        if isinstance(m, dict):
            role = str(m.get("role") or "unknown")
            roles[role] = roles.get(role, 0) + 1
    return {"total": len(messages), "roles": roles}


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
        model: str,
        memory_mode: str,
        stream: bool,
    ) -> ContextManager[_TurnObservation]:
        """Return a context manager that wraps one agent chat turn."""

    async def flush(self) -> None:
        """Flush queued observability events."""


# ---------------------------------------------------------------------------
# Noop runtime — preserves existing behaviour when observability is off.
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
        model: str = "",
        memory_mode: str = "",
        stream: bool = False,
    ):
        yield _TurnObservation()

    async def flush(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Langfuse-backed runtime
# ---------------------------------------------------------------------------

class LangfuseObservabilityRuntime:
    """Langfuse-backed observability runtime."""

    enabled = True

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self._client: Any = None
        self._propagate_attributes: Any = None

    def create_client(self, client_kwargs: dict[str, Any]) -> AsyncOpenAI:
        self._configure_environment()
        self._ensure_langfuse_client()
        from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI

        return LangfuseAsyncOpenAI(**client_kwargs)

    @contextmanager
    def agent_turn(
        self,
        *,
        user_id: str,
        model: str,
        memory_mode: str,
        stream: bool,
    ):
        """Create a span for the agent turn and yield an observation object."""
        observation = _TurnObservation()
        try:
            langfuse_client = self._ensure_langfuse_client()

            with langfuse_client.start_as_current_observation(
                as_type="span",
                name="xagent.chat",
            ) as span:
                observation.span = span

                # Apply tags via propagate_attributes
                propagate_attrs = self._get_propagate_attributes()
                if propagate_attrs is not None:
                    tags = [
                        "xagent",
                        "chat",
                        f"model:{model}",
                        f"memory:{memory_mode}",
                        "stream" if stream else "non-stream",
                    ]
                    try:
                        with propagate_attrs(user_id=user_id, tags=tags):
                            yield observation
                    except Exception:
                        yield observation
                else:
                    yield observation
        except Exception as exc:
            logger.warning("Failed to initialize Langfuse observation: %s", exc)
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

    def _configure_environment(self) -> None:
        _set_env("LANGFUSE_PUBLIC_KEY", self.config.get("public_key"))
        _set_env("LANGFUSE_SECRET_KEY", self.config.get("secret_key"))

        base_url = self.config.get("base_url")
        if base_url:
            _set_env("LANGFUSE_BASE_URL", base_url)
            _set_env("LANGFUSE_HOST", base_url)

        if "sample_rate" in self.config:
            _set_env("LANGFUSE_SAMPLE_RATE", str(self.config["sample_rate"]))
        if "debug" in self.config:
            _set_env("LANGFUSE_DEBUG", _bool_env(self.config["debug"]))
        if "tracing_enabled" in self.config:
            _set_env("LANGFUSE_TRACING_ENABLED", _bool_env(self.config["tracing_enabled"]))

    def _ensure_langfuse_client(self) -> Any:
        if self._client is None:
            self._configure_environment()
            from langfuse import get_client

            self._client = get_client()
        return self._client

    def _get_propagate_attributes(self) -> Any:
        if self._propagate_attributes is None:
            try:
                from langfuse import propagate_attributes
            except ImportError:
                return None
            self._propagate_attributes = propagate_attributes
        return self._propagate_attributes


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_observability_runtime(config: Optional[dict[str, Any]]) -> ObservabilityRuntime:
    if not isinstance(config, dict) or not config.get("enabled"):
        return NoopObservabilityRuntime()

    provider = str(config.get("provider") or "").strip().lower()
    if provider != "langfuse":
        return NoopObservabilityRuntime()
    return LangfuseObservabilityRuntime(config)


def _set_env(name: str, value: Any) -> None:
    if value is not None and str(value).strip():
        os.environ[name] = str(value).strip()


def _bool_env(value: bool) -> str:
    return "true" if value else "false"
