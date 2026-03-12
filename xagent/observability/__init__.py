"""
xAgent observability layer.

Provides a transparent ``@observe`` decorator that delegates to Langfuse when
the optional ``langfuse`` package is installed.  When Langfuse is *not*
available the decorator is a no-op — no import errors, no behaviour change.

Usage
-----
::

    from xagent.observability import observe, get_openai_client

    @observe
    async def my_function(...):
        ...

    # Get an OpenAI client that is optionally wrapped by Langfuse
    client = get_openai_client()
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from langfuse import observe as _langfuse_observe  # type: ignore[import-untyped]
    from langfuse.openai import AsyncOpenAI as _LangfuseAsyncOpenAI  # type: ignore[import-untyped]

    _LANGFUSE_AVAILABLE = True
except ImportError:
    _LANGFUSE_AVAILABLE = False
    _LangfuseAsyncOpenAI = None  # type: ignore[assignment]


def observe(func=None):  # type: ignore[return]
    """
    Observability decorator.

    When Langfuse is installed, this wraps the function with
    ``langfuse.observe``.  Otherwise the original function is returned
    unchanged so callers always receive a valid callable.
    """
    if func is None:
        # Called as ``@observe()`` with parentheses — return a decorator
        def decorator(f):  # type: ignore[return]
            return observe(f)
        return decorator

    if _LANGFUSE_AVAILABLE:
        return _langfuse_observe(func)
    return func


def get_openai_client(client=None, **kwargs):
    """
    Return an OpenAI ``AsyncOpenAI`` client.

    If Langfuse is available the client is wrapped so that API calls are
    automatically traced.  Falls back to the standard ``openai.AsyncOpenAI``
    when Langfuse is absent.

    Parameters
    ----------
    client:
        Pre-built client to return as-is.  Useful when the caller constructs
        the client itself.
    **kwargs:
        Forwarded to ``AsyncOpenAI()`` when creating a new client.
    """
    if client is not None:
        return client

    if _LANGFUSE_AVAILABLE and _LangfuseAsyncOpenAI is not None:
        return _LangfuseAsyncOpenAI(**kwargs)

    # Standard openai client — imported lazily to avoid hard import at
    # module level when langfuse is the primary path.
    from openai import AsyncOpenAI  # type: ignore[import-untyped]
    return AsyncOpenAI(**kwargs)


__all__ = ["observe", "get_openai_client", "_LANGFUSE_AVAILABLE"]
