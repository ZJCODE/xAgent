"""Interface exports."""

from __future__ import annotations

import importlib

__all__ = ["AgentHTTPServer", "AgentCLI"]

_EXPORTS = {
    "AgentHTTPServer": (".server", "AgentHTTPServer"),
    "AgentCLI": (".cli", "AgentCLI"),
}


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
