"""Compatibility helpers for optional OpenAI and Langfuse dependencies."""

from __future__ import annotations

from typing import Any, Callable


def _noop_observe(*args: Any, **kwargs: Any) -> Callable:
    """Fallback observe decorator when Langfuse is unavailable."""

    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]

    def decorator(func: Callable) -> Callable:
        return func

    return decorator


try:
    from langfuse import observe as observe
except ModuleNotFoundError:
    observe = _noop_observe


def _load_async_openai() -> type:
    """Load the preferred AsyncOpenAI client implementation lazily."""

    try:
        from langfuse.openai import AsyncOpenAI as LangfuseAsyncOpenAI

        return LangfuseAsyncOpenAI
    except ModuleNotFoundError:
        pass

    try:
        from openai import AsyncOpenAI as OpenAIAsyncOpenAI

        return OpenAIAsyncOpenAI
    except ModuleNotFoundError:
        pass

    class MissingAsyncOpenAI:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise ImportError(
                "OpenAI client dependencies are not installed. "
                "Install `openai` or `langfuse` to enable model-backed features."
            )

    return MissingAsyncOpenAI


AsyncOpenAI = _load_async_openai()

__all__ = ["AsyncOpenAI", "observe"]
