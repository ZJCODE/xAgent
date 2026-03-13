"""
xagent/core — lazy-loading package.

Exports all public symbols from the core sub-modules without triggering
heavy import chains (e.g. chromadb / langfuse) at import time.
"""

import importlib

__all__ = [
    "Agent",
    "AgentConfig",
    "normalize_session_id",
    "ModelCaller",
    "ReplyType",
    "ToolExecutor",
    "MCPManager",
    "ImageProcessor",
]

_EXPORTS = {
    "Agent":               (".agent",          "Agent"),
    "AgentConfig":         (".agent",          "AgentConfig"),
    "normalize_session_id":(".session",        "normalize_session_id"),
    "ModelCaller":         (".model_caller",   "ModelCaller"),
    "ReplyType":           (".model_caller",   "ReplyType"),
    "ToolExecutor":        (".tool_executor",  "ToolExecutor"),
    "MCPManager":          (".mcp_manager",    "MCPManager"),
    "ImageProcessor":      (".image_processor","ImageProcessor"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
