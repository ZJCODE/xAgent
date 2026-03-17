"""Utilities for xAgent package."""

import importlib

__all__ = ["function_tool", "MCPTool", "file_to_data_uri"]

_EXPORTS = {
    "function_tool": (".tool_decorator", "function_tool"),
    "MCPTool": (".mcp_convertor", "MCPTool"),
    "file_to_data_uri": (".image_utils", "file_to_data_uri"),
}


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_name, attr_name = _EXPORTS[name]
    module = importlib.import_module(module_name, __name__)
    value = getattr(module, attr_name)
    globals()[name] = value
    return value
