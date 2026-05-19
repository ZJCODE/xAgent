"""Optional Langfuse runtime wiring for OpenAI-compatible clients."""

from __future__ import annotations

import inspect
import logging
import os
from contextlib import ExitStack, contextmanager
from typing import Any, ContextManager, Optional, Protocol

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


class ObservabilityRuntime(Protocol):
    """Small interface used by the agent runtime for optional tracing."""

    enabled: bool

    def create_client(self, client_kwargs: dict[str, Any]) -> Optional[AsyncOpenAI]:
        """Create an OpenAI-compatible async client."""

    def agent_turn(
        self,
        *,
        user_id: str,
        model: str,
        memory_mode: str,
        stream: bool,
    ) -> ContextManager[None]:
        """Return a context manager for one agent chat turn."""

    async def flush(self) -> None:
        """Flush queued observability events."""


class NoopObservabilityRuntime:
    """Default observability runtime that preserves existing behavior."""

    enabled = False

    def create_client(self, client_kwargs: dict[str, Any]) -> Optional[AsyncOpenAI]:
        if not client_kwargs:
            return None
        return AsyncOpenAI(**client_kwargs)

    @contextmanager
    def agent_turn(
        self,
        *,
        user_id: str,
        model: str,
        memory_mode: str,
        stream: bool,
    ):
        yield

    async def flush(self) -> None:
        return None


class LangfuseObservabilityRuntime:
    """Langfuse-backed observability runtime."""

    enabled = True

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = dict(config)
        self._client = None
        self._propagate_attributes = None

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
        with ExitStack() as stack:
            try:
                langfuse_client = self._ensure_langfuse_client()
                starter = getattr(langfuse_client, "start_as_current_observation", None)
                if starter is not None:
                    stack.enter_context(starter(as_type="span", name="xagent.chat"))

                propagate_attributes = self._get_propagate_attributes()
                if propagate_attributes is not None:
                    tags = [
                        "xagent",
                        "chat",
                        f"model:{model}",
                        f"memory:{memory_mode}",
                        "stream" if stream else "non-stream",
                    ]
                    stack.enter_context(
                        propagate_attributes(
                            user_id=user_id,
                            tags=tags,
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to start Langfuse observation: %s", exc)

            yield

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

    def _ensure_langfuse_client(self):
        if self._client is None:
            self._configure_environment()
            from langfuse import get_client

            self._client = get_client()
        return self._client

    def _get_propagate_attributes(self):
        if self._propagate_attributes is None:
            try:
                from langfuse import propagate_attributes
            except ImportError:
                return None
            self._propagate_attributes = propagate_attributes
        return self._propagate_attributes


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