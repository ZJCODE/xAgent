"""Optional Langfuse runtime wiring for OpenAI-compatible and Anthropic clients."""

from __future__ import annotations

import inspect
import json
import logging
import os
import uuid
from contextlib import contextmanager
from typing import Any, ContextManager, Optional, Protocol

from openai import AsyncOpenAI


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Observation objects — yielded by context managers so callers can populate
# input / output / usage after the observation is created.
# ---------------------------------------------------------------------------

class _TurnObservation:
    """Holder for trace / span references so Agent can set input and output."""

    def __init__(self) -> None:
        self.span: Any = None

    def set_input(self, messages: list) -> None:
        if self.span is not None:
            try:
                self.span.update(input=_summarize_messages(messages))
            except Exception:
                pass

    def set_output(self, content: str, tool_results: Optional[list] = None) -> None:
        if self.span is not None:
            output: dict[str, Any] = {"content": content}
            if tool_results:
                output["tool_results"] = tool_results
            try:
                self.span.update(output=output)
            except Exception:
                pass


class _LLMCallObservation:
    """Holder for a generation observation so the caller can set output / usage."""

    def __init__(self, generation: Any = None) -> None:
        self.generation = generation

    def set_output(self, content: Optional[str] = None, tool_calls: Optional[list] = None) -> None:
        gen = self.generation
        if gen is None:
            return
        output: dict[str, Any] = {}
        if content:
            output["content"] = content
        if tool_calls:
            output["tool_calls"] = [
                {
                    "name": _field(tc, "name") or "",
                    "arguments": _field(tc, "arguments") or "",
                }
                for tc in tool_calls
            ]
        if output:
            try:
                gen.update(output=output)
            except Exception:
                pass

    def set_usage(self, usage: dict) -> None:
        gen = self.generation
        if gen is None or not usage:
            return
        gen_usage: dict[str, int] = {}
        inp = usage.get("input_tokens") or usage.get("prompt_tokens")
        out = usage.get("output_tokens") or usage.get("completion_tokens")
        total = usage.get("total_tokens")
        if inp is not None:
            gen_usage["input"] = int(inp)
        if out is not None:
            gen_usage["output"] = int(out)
        if total is not None:
            gen_usage["total"] = int(total)
        if gen_usage:
            try:
                gen.update(usage=gen_usage)
            except Exception:
                pass


class _ToolCallObservation:
    """Holder for a tool-call span so the caller can record success or error."""

    def __init__(self, span: Any = None) -> None:
        self.span = span

    def set_success(self, result: Any) -> None:
        if self.span is not None:
            try:
                self.span.update(output=_safe_output(result))
            except Exception:
                pass

    def set_error(self, error: str) -> None:
        if self.span is not None:
            try:
                self.span.update(
                    output={"error": error},
                    level="ERROR",
                    status_message=error,
                )
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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


def _safe_output(result: Any) -> str:
    """Truncate a tool result to a reasonable size for Langfuse display."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, (dict, list)):
        try:
            text = json.dumps(result, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(result)
    else:
        text = str(result)
    return text[:2000] if len(text) > 2000 else text


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------

class ObservabilityRuntime(Protocol):
    """Interface used by the agent runtime for optional tracing."""

    enabled: bool

    def create_client(self, client_kwargs: dict[str, Any]) -> Optional[AsyncOpenAI]:
        """Create an OpenAI-compatible async client (possibly wrapped)."""
        ...

    def agent_turn(
        self,
        *,
        user_id: str,
        model: str,
        memory_mode: str,
        stream: bool,
    ) -> ContextManager[_TurnObservation]:
        """Return a context manager that wraps one agent chat turn."""

    def trace_llm_call(
        self,
        *,
        model: str,
        input_summary: Optional[dict] = None,
    ) -> ContextManager[_LLMCallObservation]:
        """Return a context manager wrapping a single LLM call.

        Used for Anthropic calls that lack auto-instrumentation from
        ``langfuse.openai.AsyncOpenAI``.
        """

    def trace_tool_call(
        self,
        *,
        tool_name: str,
        args: Optional[dict] = None,
    ) -> ContextManager[_ToolCallObservation]:
        """Return a context manager wrapping a single tool execution."""

    def record_score(
        self,
        *,
        name: str,
        value: float,
        comment: Optional[str] = None,
    ) -> None:
        """Record an evaluation score on the current trace."""

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

    @contextmanager
    def trace_llm_call(
        self,
        *,
        model: str = "",
        input_summary: Optional[dict] = None,
    ):
        yield _LLMCallObservation()

    @contextmanager
    def trace_tool_call(
        self,
        *,
        tool_name: str = "",
        args: Optional[dict] = None,
    ):
        yield _ToolCallObservation()

    def record_score(
        self,
        *,
        name: str = "",
        value: float = 0.0,
        comment: Optional[str] = None,
    ) -> None:
        return None

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
        self._langfuse_context: Any = None
        self._update_trace_fn: Any = None
        self._session_id: str = (
            str(config.get("session_id") or "").strip()
            or str(uuid.uuid4())
        )

    # -- client creation ---------------------------------------------------

    def create_client(self, client_kwargs: dict[str, Any]) -> AsyncOpenAI:
        self._configure_environment()
        self._ensure_langfuse_client()
        from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI

        return LangfuseAsyncOpenAI(**client_kwargs)

    # -- agent turn --------------------------------------------------------

    @contextmanager
    def agent_turn(
        self,
        *,
        user_id: str,
        model: str,
        memory_mode: str,
        stream: bool,
    ):
        """Create a span for the agent turn and set up auto-instrumentation nesting."""
        observation = _TurnObservation()
        try:
            langfuse_client = self._ensure_langfuse_client()

            with langfuse_client.start_as_current_observation(
                as_type="span",
                name="agent_turn",
                input={"model": model, "memory_mode": memory_mode},
            ) as span:
                observation.span = span

                # Update the root trace with user/session/metadata
                self._update_trace_metadata(
                    user_id=user_id,
                    model=model,
                    memory_mode=memory_mode,
                    stream=stream,
                )

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

    # -- LLM call generation -----------------------------------------------

    @contextmanager
    def trace_llm_call(
        self,
        *,
        model: str,
        input_summary: Optional[dict] = None,
    ):
        """Create a generation observation for an LLM call.

        Intended for the Anthropic path where ``langfuse.openai.AsyncOpenAI``
        auto-instrumentation is not available.
        """
        observation = _LLMCallObservation()
        try:
            langfuse_client = self._ensure_langfuse_client()
            gen_kwargs: dict[str, Any] = {"name": "llm_call", "model": model}
            if input_summary:
                gen_kwargs["input"] = input_summary

            with langfuse_client.start_as_current_observation(
                as_type="generation",
                **gen_kwargs,
            ) as gen:
                observation.generation = gen
                yield observation
        except Exception as exc:
            logger.warning("Failed to start LLM call observation: %s", exc)
            yield observation

    # -- tool call span ----------------------------------------------------

    @contextmanager
    def trace_tool_call(
        self,
        *,
        tool_name: str,
        args: Optional[dict] = None,
    ):
        """Create a child span for a single tool execution."""
        observation = _ToolCallObservation()
        try:
            langfuse_client = self._ensure_langfuse_client()
            with langfuse_client.start_as_current_observation(
                as_type="span",
                name=f"tool.{tool_name}",
                input={"tool_name": tool_name, "args": args},
            ) as span:
                observation.span = span
                yield observation
        except Exception as exc:
            logger.warning("Failed to start tool observation: %s", exc)
            yield observation

    # -- scoring -----------------------------------------------------------

    def record_score(
        self,
        *,
        name: str,
        value: float,
        comment: Optional[str] = None,
    ) -> None:
        """Attach a score to the current trace."""
        try:
            langfuse_client = self._ensure_langfuse_client()
            ctx = self._get_langfuse_context()
            if ctx is None:
                return
            trace_id = ctx.get_current_trace_id()
            if trace_id:
                score_kwargs: dict[str, Any] = {
                    "trace_id": trace_id,
                    "name": name,
                    "value": value,
                }
                if comment:
                    score_kwargs["comment"] = comment
                langfuse_client.score(**score_kwargs)
        except Exception as exc:
            logger.warning("Failed to record Langfuse score: %s", exc)

    # -- flush -------------------------------------------------------------

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

    # -- internals ---------------------------------------------------------

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
            self._update_trace_fn = getattr(
                self._client, "update_current_trace", None
            )
        return self._client

    def _get_propagate_attributes(self) -> Any:
        if self._propagate_attributes is None:
            try:
                from langfuse import propagate_attributes
            except ImportError:
                return None
            self._propagate_attributes = propagate_attributes
        return self._propagate_attributes

    def _get_langfuse_context(self) -> Any:
        if self._langfuse_context is None:
            try:
                from langfuse import langfuse_context
            except ImportError:
                self._langfuse_context = False
            else:
                self._langfuse_context = langfuse_context
        return self._langfuse_context if self._langfuse_context is not False else None

    def _update_trace_metadata(
        self,
        *,
        user_id: str,
        model: str,
        memory_mode: str,
        stream: bool,
    ) -> None:
        """Set metadata on the root trace (best-effort)."""
        if self._update_trace_fn is None:
            return
        try:
            self._update_trace_fn(
                name="xagent.chat",
                user_id=user_id,
                session_id=self._session_id,
                metadata={
                    "model": model,
                    "memory_mode": memory_mode,
                    "stream": stream,
                    "environment": self.config.get("environment", "production"),
                    "release": self.config.get("release", os.environ.get("XAGENT_VERSION", "")),
                },
            )
        except Exception:
            pass  # trace-level metadata is a nice-to-have, not critical


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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _set_env(name: str, value: Any) -> None:
    if value is not None and str(value).strip():
        os.environ[name] = str(value).strip()


def _bool_env(value: bool) -> str:
    return "true" if value else "false"
